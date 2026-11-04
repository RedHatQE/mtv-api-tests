"""Bounded, never-raising cluster state snapshots for failure and session-start evidence."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, Literal

from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from utilities.data_collector import data_collector_path, redact_secret, utc_timestamp

LOGGER = get_logger(name=__name__)

_COMMAND_TIMEOUT_SECONDS = 20
# Per-PVC YAML can be tens of KB each on a full disk; the listing above already carries every phase.
_MAX_PENDING_PVC_DUMPS = 20
_MCO_NAMESPACE = "openshift-machine-config-operator"
_STORAGE_NAMESPACE = "openshift-storage"
_MTV_NAMESPACE = "openshift-mtv"
_CNV_NAMESPACE = "openshift-cnv"
_CSI_PROVISIONER_DEPLOYMENT = "csi-rbdplugin-provisioner"
_CSI_LOG_TAIL_LINES = 3000
_MCO_LOG_TAIL_LINES = 500
_REASON_SLUG_MAX_LENGTH = 60
_REASON_SLUG_SEPARATOR = "-"
_UNSPECIFIED_REASON = "unspecified"
_EXIT_CODE_LABEL = "exit_code:"
_STDOUT_MARKER = "--- stdout ---"
_STDERR_MARKER = "--- stderr ---"
_SECTION_SEPARATOR = "\n\n"
_CLUSTER_STATE_DIRECTORY = "cluster-state"
_SESSION_START_DIRECTORY = "session-start"
_SESSION_START_CLUSTER_NAME = "cluster.txt"
_SESSION_START_RUN_IDENTITY_NAME = "run-identity.txt"
_GIT_HEAD_FALLBACK = "unknown"


def _run_bounded(cmd: list[str], timeout: int = _COMMAND_TIMEOUT_SECONDS) -> str:
    """Run one command with a bounded wait and render its exit code and both streams.

    Args:
        cmd (list[str]): Command and arguments, run without a shell.
        timeout (int): Seconds to wait before the command is abandoned.

    Returns:
        str: Command line, exit code, stdout and stderr; a failure note instead when the command
            could not be started or did not finish in time.
    """
    header = f"$ {' '.join(cmd)}"
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return f"{header}\n[command failed after up to {timeout}s: {exc}]"
    return (
        f"{header}\n{_EXIT_CODE_LABEL} {result.returncode}\n"
        f"{_STDOUT_MARKER}\n{result.stdout}\n{_STDERR_MARKER}\n{result.stderr}"
    )


def _stdout_of(record: str) -> str:
    """Return the stdout section of a :func:`_run_bounded` record.

    Args:
        record (str): Record produced by :func:`_run_bounded`.

    Returns:
        str: Captured stdout, or an empty string when the record holds no stdout section.
    """
    return record.partition(f"{_STDOUT_MARKER}\n")[2].partition(f"\n{_STDERR_MARKER}")[0]


def _succeeded(record: str) -> bool:
    """Report whether a :func:`_run_bounded` record shows a zero exit code.

    Args:
        record (str): Record produced by :func:`_run_bounded`.

    Returns:
        bool: True when the command ran and exited with status 0.
    """
    return f"\n{_EXIT_CODE_LABEL} 0\n" in record


def _reason_slug(reason: str) -> str:
    """Convert a failure reason to a file-name slug.

    Args:
        reason (str): Failure reason as written in the snapshot header.

    Returns:
        str: Lower-case, dash-separated, length-bounded slug.
    """
    slug = re.sub(r"[^a-z0-9]+", _REASON_SLUG_SEPARATOR, reason.lower()).strip(_REASON_SLUG_SEPARATOR)
    return (slug or _UNSPECIFIED_REASON)[:_REASON_SLUG_MAX_LENGTH]


def _worker_suffix() -> str:
    """Return the pytest-xdist worker id as a file-name suffix.

    Returns:
        str: ``-<worker id>`` in a parallel run, an empty string otherwise.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    return f"-{worker}" if worker else ""


def _mcp_pool_summary() -> str:
    """Render the counters and conditions of every MachineConfigPool.

    Returns:
        str: One text block per pool, or the raw command record when the JSON cannot be parsed.
    """
    record = _run_bounded(["oc", "get", "mcp", "-o", "json"])
    try:
        # MachineConfigPool JSON is dynamic, provider-shaped data: the CRD schema does not express
        # the per-field types, so each field is validated with ``.get()`` below.
        pools: list[dict[str, Any]] = json.loads(_stdout_of(record))["items"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        LOGGER.warning(f"Could not parse the MachineConfigPool JSON, keeping the raw output: {exc}")
        return record

    lines: list[str] = []
    for pool in pools:
        metadata = pool.get("metadata") or {}
        status = pool.get("status") or {}
        lines.append(
            f"{metadata.get('name', '<unnamed>')}: ready={status.get('readyMachineCount')} "
            f"updated={status.get('updatedMachineCount')} degraded={status.get('degradedMachineCount')} "
            f"unavailable={status.get('unavailableMachineCount')}"
        )
        for condition in status.get("conditions") or []:
            lines.append(
                f"  {condition.get('type')}={condition.get('status')} "
                f"reason={condition.get('reason')} message={condition.get('message')}"
            )
    return "\n".join(lines)


def _dump_upgrade_scope() -> list[str]:
    """Collect the MachineConfig, MachineConfigPool, operator and image state of an upgrade failure.

    Returns:
        list[str]: One rendered command record per probe.
    """
    return [
        _run_bounded(["oc", "get", "clusterversion", "-o", "yaml"]),
        _run_bounded(["oc", "get", "co"]),
        _run_bounded(["oc", "get", "co", "machine-config", "-o", "yaml"]),
        _run_bounded(["oc", "get", "mcp", "-o", "yaml"]),
        _mcp_pool_summary(),
        _run_bounded(["oc", "get", "nodes", "-o", "wide"]),
        _run_bounded(["oc", "-n", _MCO_NAMESPACE, "get", "pods", "-o", "wide"]),
        _run_bounded([
            "oc",
            "-n",
            _MCO_NAMESPACE,
            "logs",
            "deploy/machine-config-operator",
            f"--tail={_MCO_LOG_TAIL_LINES}",
        ]),
        _run_bounded([
            "oc",
            "-n",
            _MCO_NAMESPACE,
            "logs",
            "daemonset/machine-config-daemon",
            f"--tail={_MCO_LOG_TAIL_LINES}",
        ]),
        _run_bounded(["oc", "get", "idms"]),
        _run_bounded(["oc", "get", "icsp"]),
        _run_bounded(["oc", "get", "image.config", "cluster", "-o", "yaml"]),
        _run_bounded(["oc", "-n", _MTV_NAMESPACE, "get", "csv"]),
    ]


def _pending_pvc_yaml(namespace: str) -> list[str]:
    """Render full YAML for the non-Bound PVCs of a namespace only.

    Args:
        namespace (str): Namespace holding the PVCs.

    Returns:
        list[str]: The PVC listing record plus one record per non-Bound PVC; only the listing
            record when the listing cannot be parsed.
    """
    listing = _run_bounded(["oc", "-n", namespace, "get", "pvc", "-o", "json"])
    try:
        pending_names = [
            item["metadata"]["name"]
            for item in json.loads(_stdout_of(listing))["items"]
            if (item.get("status") or {}).get("phase") != "Bound"
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        LOGGER.warning(f"Could not list the PVCs of namespace '{namespace}', keeping the raw output: {exc}")
        return [listing]

    return [
        listing,
        *(
            _run_bounded(["oc", "-n", namespace, "get", "pvc", name, "-o", "yaml"])
            for name in pending_names[:_MAX_PENDING_PVC_DUMPS]
        ),
    ]


def _dump_storage_scope(namespace: str) -> list[str]:
    """Collect PVC, DataVolume, event and ceph-csi provisioner state of a stalled migration.

    Args:
        namespace (str): Migration target namespace.

    Returns:
        list[str]: One rendered command record per probe.
    """
    sections = [
        _run_bounded(["oc", "-n", namespace, "get", "pvc,dv,pv", "-o", "wide"]),
        *_pending_pvc_yaml(namespace=namespace),
        _run_bounded(["oc", "-n", namespace, "get", "events", "--sort-by=.lastTimestamp"]),
        _run_bounded(["oc", "get", "sc", "-o", "wide"]),
        _run_bounded(["oc", "-n", _STORAGE_NAMESPACE, "get", "pods", "-o", "wide"]),
    ]
    provisioner_logs = [
        "oc",
        "-n",
        _STORAGE_NAMESPACE,
        "logs",
        f"deploy/{_CSI_PROVISIONER_DEPLOYMENT}",
        "--all-containers",
        f"--tail={_CSI_LOG_TAIL_LINES}",
    ]
    # The current log always: it holds the errors produced at dump time even when the container
    # restarted. The previous container's log is added only when one exists.
    sections.append(_run_bounded(provisioner_logs))
    previous_logs = _run_bounded([*provisioner_logs, "--previous"])
    if _succeeded(previous_logs):
        sections.append(previous_logs)
    return sections


def _write_snapshot(*, collector_path: Path, scope: str, reason: str, sections: list[str]) -> Path:
    """Write one snapshot file.

    Args:
        collector_path (Path): Data-collector root directory.
        scope (str): Snapshot scope, used in the file name and header.
        reason (str): Failure reason, used in the file name and header.
        sections (list[str]): Rendered command records, written in order.

    Returns:
        Path: Path of the written snapshot.
    """
    directory = collector_path / _CLUSTER_STATE_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_path = directory / f"{utc_timestamp()}-{scope}-{_reason_slug(reason)}{_worker_suffix()}.txt"
    header = f"# reason: {reason}\n# scope: {scope}\n# collected: {utc_timestamp()}"
    snapshot_path.write_text(f"{header}\n\n{_SECTION_SEPARATOR.join(sections)}\n")
    return snapshot_path


def _collect_and_write(*, reason: str, scope: str, collect: Callable[[], list[str]]) -> None:
    """Write one snapshot for a scope, skipping it when collection is disabled and never raising.

    Args:
        reason (str): Failure reason, used in the file name and header.
        scope (str): Snapshot scope, used in the file name and header.
        collect (Callable[[], list[str]]): Probe collector of the scope, called only when
            collection is enabled.
    """
    collector_path = data_collector_path()
    if collector_path is None:
        return

    try:
        snapshot_path = _write_snapshot(
            collector_path=collector_path,
            scope=scope,
            reason=reason,
            sections=collect(),
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        LOGGER.warning(f"Cluster state snapshot for scope '{scope}' (reason '{reason}') failed: {exc}")
        return

    LOGGER.warning(f"Cluster state snapshot written to {snapshot_path}")


def dump_cluster_state(
    *,
    reason: str,
    scope: Literal["upgrade", "storage"],
    namespace: str | None = None,
) -> None:
    """Write a bounded cluster-state snapshot for a real failure.

    Called immediately before a failure is raised, so the original exception stays the error the
    caller sees: collection is best effort, every command is time-bounded, and nothing escapes.

    Args:
        reason (str): Short cause, used as the file-name slug and the file header.
        scope (Literal["upgrade", "storage"]): Which snapshot to collect.
        namespace (str | None): Migration target namespace, required by the storage scope.

    Raises:
        ValueError: If the storage scope is requested without a namespace.
    """
    if scope == "storage":
        if namespace is None:
            raise ValueError("dump_cluster_state with scope='storage' requires the migration target namespace")
        _collect_and_write(reason=reason, scope=scope, collect=partial(_dump_storage_scope, namespace=namespace))
        return

    _collect_and_write(reason=reason, scope=scope, collect=_dump_upgrade_scope)


def _session_start_cluster_sections() -> list[str]:
    """Collect the pre-run cluster state used as the run baseline.

    Returns:
        list[str]: One rendered command record per probe.
    """
    return [
        _run_bounded(["oc", "get", "clusterversion", "-o", "yaml"]),
        _run_bounded(["oc", "get", "co"]),
        _run_bounded(["oc", "get", "mcp", "-o", "yaml"]),
        _mcp_pool_summary(),
        _run_bounded(["oc", "get", "nodes", "-o", "wide"]),
        _run_bounded(["oc", "-n", _MTV_NAMESPACE, "get", "csv"]),
        _run_bounded(["oc", "-n", _CNV_NAMESPACE, "get", "csv"]),
    ]


def _repository_root() -> Path:
    """Return the checkout this module is imported from.

    Returns:
        Path: Repository root candidate; it has no ``.git`` when the suite runs installed.
    """
    return Path(__file__).resolve().parents[1]


def _git_head() -> str:
    """Read the checkout's current revision.

    Returns:
        str: ``git rev-parse HEAD`` output, or ``unknown`` when the checkout has no ``.git``
            or the command fails.
    """
    repository_root = _repository_root()
    if not (repository_root / ".git").exists():
        return _GIT_HEAD_FALLBACK

    record = _run_bounded(["git", "-C", str(repository_root), "rev-parse", "HEAD"])
    if not _succeeded(record):
        LOGGER.warning(f"Could not read the checkout revision from {repository_root}, recording {_GIT_HEAD_FALLBACK}")
        return _GIT_HEAD_FALLBACK
    return _stdout_of(record).strip()


def _session_start_identity_sections(
    *, pytest_args: Sequence[str], skip_teardown: bool, skip_data_collector: bool
) -> list[str]:
    """Collect the run identity: checkout revision, pytest invocation and skip flags.

    Args:
        pytest_args (Sequence[str]): Arguments pytest was invoked with, including ``--tc`` overrides.
        skip_teardown (bool): Whether ``--skip-teardown`` was set.
        skip_data_collector (bool): Whether ``--skip-data-collector`` was set.

    Returns:
        list[str]: One rendered record per identity probe.
    """
    command_line = " ".join(pytest_args)
    # The invocation can carry --tc=cluster_password:<password>, so redact before writing. The key
    # is required: a missing one aborts instead of writing the password unredacted.
    command_line = redact_secret(text=command_line, secret=py_config["cluster_password"])
    return [
        f"$ git rev-parse HEAD\n{_git_head()}",
        f"$ pytest\n{command_line}",
        _run_bounded(["oc", "whoami"]),
        f"# skip_teardown: {skip_teardown}\n# skip_data_collector: {skip_data_collector}",
    ]


def _write_once_atomic(path: Path, text: str) -> bool:
    """Publish a complete file only when the destination does not exist yet.

    Every xdist worker writes the same session-start files. The content is written to a
    process-unique temporary sibling and hard-linked into place, so the file appears complete and
    the first writer wins instead of a later worker overwriting the true start-of-run snapshot.

    Args:
        path (Path): Destination file.
        text (str): Content to write.

    Returns:
        bool: True when this call created the file, False when a snapshot already existed.
    """
    temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(text)
    try:
        os.link(temporary_path, path)
    except FileExistsError:
        return False
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def write_session_baseline(*, pytest_args: Sequence[str], skip_teardown: bool, skip_data_collector: bool) -> None:
    """Write the session-start cluster state and run identity once per session, never raising.

    Args:
        pytest_args (Sequence[str]): Arguments pytest was invoked with, including ``--tc`` overrides.
        skip_teardown (bool): Whether ``--skip-teardown`` was set.
        skip_data_collector (bool): Whether ``--skip-data-collector`` was set.
    """
    if skip_data_collector:
        return

    collector_path = data_collector_path()
    if collector_path is None:
        return

    directory = collector_path / _SESSION_START_DIRECTORY
    cluster_path = directory / _SESSION_START_CLUSTER_NAME
    identity_path = directory / _SESSION_START_RUN_IDENTITY_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if cluster_path.exists() and identity_path.exists():
            LOGGER.info(f"Session-start baseline already exists in {directory}, keeping the first snapshot")
            return
        cluster_created = _write_once_atomic(
            path=cluster_path,
            text=f"{_SECTION_SEPARATOR.join(_session_start_cluster_sections())}\n",
        )
        identity_sections = _session_start_identity_sections(
            pytest_args=pytest_args,
            skip_teardown=skip_teardown,
            skip_data_collector=skip_data_collector,
        )
        identity_created = _write_once_atomic(
            path=identity_path, text=f"{_SECTION_SEPARATOR.join(identity_sections)}\n"
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        # Best-effort evidence: a missing required config key skips the baseline instead of
        # aborting session start or writing the run identity unredacted.
        LOGGER.warning(f"Session-start baseline could not be written to {directory}: {exc}")
        return

    if cluster_created and identity_created:
        LOGGER.warning(f"Session-start baseline written to {directory}")
        return
    LOGGER.info(f"Session-start baseline already exists in {directory}, keeping the first snapshot")
