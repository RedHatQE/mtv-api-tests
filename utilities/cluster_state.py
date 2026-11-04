"""Bounded, never-raising cluster state snapshots for failure and session-start evidence."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

import yaml
from simple_logger.logger import get_logger

from utilities.data_collector import data_collector_path, redact_secret, utc_timestamp
from utilities.utils import resolve_cluster_credential

if TYPE_CHECKING:
    from kubernetes.client import Configuration
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(name=__name__)

_OC_BINARY = "oc"
_COMMAND_TIMEOUT_SECONDS = 20
# A snapshot must never delay the failure it documents: the storage scope alone can run ~25 probes,
# so a hanging cluster would otherwise cost minutes of per-command timeouts before the error shows.
_SNAPSHOT_DEADLINE_SECONDS = 120
# Per-PVC YAML can be tens of KB each on a full disk; the listing above already carries every phase.
_MAX_PENDING_PVC_DUMPS = 20
_MCO_NAMESPACE = "openshift-machine-config-operator"
_STORAGE_NAMESPACE = "openshift-storage"
_MTV_NAMESPACE = "openshift-mtv"
_CNV_NAMESPACE = "openshift-cnv"
_CSI_PROVISIONER_DEPLOYMENT = "csi-rbdplugin-provisioner"
_CSI_LOG_TAIL_LINES = 3000
_MCO_LOG_TAIL_LINES = 500
_ALL_PODS_FLAG = "--all-pods"
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
# Temporary kubeconfig built from the run client, so probes also work in a container run that has
# no ambient kubeconfig at all.
_KUBECONFIG_FILE_NAME = "kubeconfig"
_KUBECONFIG_DIRECTORY_PREFIX = "mtv-cluster-state-"
_KUBECONFIG_CONTEXT_NAME = "cluster-state-snapshot"
_AMBIENT_CONTEXT_NOTE = "ambient oc context (no server in the run client configuration)"
_KUBECONFIG_FILE_MODE = 0o600
_REDACTED_VALUE = "***"
_TC_ARGUMENT_PREFIX = "--tc="
# Credential-ish ``--tc`` keys whose value must never reach an artifact.
_CREDENTIAL_KEY_PATTERN = re.compile(r"password|token|secret|api_key|apikey|credential", re.IGNORECASE)
_URL_USERINFO_PATTERN = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/@\s]+@")


def _run_bounded(cmd: list[str], timeout: float = _COMMAND_TIMEOUT_SECONDS) -> str:
    """Run one command with a bounded wait and render its exit code and both streams.

    Args:
        cmd (list[str]): Command and arguments, run without a shell.
        timeout (float): Seconds to wait before the command is abandoned.

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
        return f"{header}\n[command failed after up to {timeout:g}s: {exc}]"
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


@dataclass
class _Probes:
    """Probe runner of one snapshot: one kubeconfig, one total deadline, one skip counter.

    Args:
        kubeconfig (Path | None): Temporary kubeconfig passed to every ``oc`` probe, or None to
            use the ambient oc context.
        started (float): ``monotonic()`` value the snapshot deadline is measured from.
        skipped (int): Probes not run because the snapshot deadline had already passed.
    """

    kubeconfig: Path | None = None
    started: float = field(default_factory=monotonic)
    skipped: int = 0

    def run(self, cmd: list[str], timeout: float = _COMMAND_TIMEOUT_SECONDS) -> str:
        """Run one probe inside the snapshot budget and render its record.

        Args:
            cmd (list[str]): Command and arguments, run without a shell.
            timeout (float): Per-command limit, further bounded by the time left in the snapshot.

        Returns:
            str: The :func:`_run_bounded` record, or a skip note once the deadline has passed.
        """
        if cmd[0] == _OC_BINARY and self.kubeconfig is not None:
            # --kubeconfig is a global flag, placed next to the binary so every subcommand accepts it.
            cmd = [cmd[0], f"--kubeconfig={self.kubeconfig}", *cmd[1:]]
        budget = min(timeout, self._remaining_seconds())
        if budget <= 0:
            self.skipped += 1
            return f"$ {' '.join(cmd)}\n[skipped: snapshot deadline of {_SNAPSHOT_DEADLINE_SECONDS}s reached]"
        return _run_bounded(cmd=cmd, timeout=budget)

    def deadline_record(self) -> str | None:
        """Render the record that closes a snapshot cut short by its deadline.

        Returns:
            str | None: One summary line, or None when the whole snapshot fit in the budget.
        """
        if not self.skipped:
            return None
        elapsed = int(monotonic() - self.started)
        return (
            f"# snapshot deadline reached after {elapsed}s of {_SNAPSHOT_DEADLINE_SECONDS}s, "
            f"{self.skipped} probes skipped"
        )

    def _remaining_seconds(self) -> float:
        """Return the time left in the snapshot budget.

        Returns:
            float: Seconds remaining, or a value of zero or less once the deadline has passed.
        """
        return self.started + _SNAPSHOT_DEADLINE_SECONDS - monotonic()


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


def _mcp_pool_summary(probes: _Probes) -> str:
    """Render the counters and conditions of every MachineConfigPool.

    Args:
        probes (_Probes): Probe runner of the current snapshot.

    Returns:
        str: One text block per pool, or the raw command record when the JSON cannot be parsed.
    """
    record = probes.run(["oc", "get", "mcp", "-o", "json"])
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


def _dump_upgrade_scope(probes: _Probes) -> list[str]:
    """Collect the MachineConfig, MachineConfigPool, operator and image state of an upgrade failure.

    Args:
        probes (_Probes): Probe runner of the current snapshot.

    Returns:
        list[str]: One rendered command record per probe.
    """
    return [
        probes.run(["oc", "get", "clusterversion", "-o", "yaml"]),
        probes.run(["oc", "get", "co"]),
        probes.run(["oc", "get", "co", "machine-config", "-o", "yaml"]),
        probes.run(["oc", "get", "mcp", "-o", "yaml"]),
        _mcp_pool_summary(probes=probes),
        probes.run(["oc", "get", "nodes", "-o", "wide"]),
        probes.run(["oc", "-n", _MCO_NAMESPACE, "get", "pods", "-o", "wide"]),
        probes.run([
            "oc",
            "-n",
            _MCO_NAMESPACE,
            "logs",
            "deploy/machine-config-operator",
            f"--tail={_MCO_LOG_TAIL_LINES}",
        ]),
        probes.run([
            "oc",
            "-n",
            _MCO_NAMESPACE,
            "logs",
            "daemonset/machine-config-daemon",
            f"--tail={_MCO_LOG_TAIL_LINES}",
        ]),
        probes.run(["oc", "get", "idms"]),
        probes.run(["oc", "get", "icsp"]),
        probes.run(["oc", "get", "image.config", "cluster", "-o", "yaml"]),
        probes.run(["oc", "-n", _MTV_NAMESPACE, "get", "csv"]),
    ]


def _pending_pvc_yaml(namespace: str, probes: _Probes) -> list[str]:
    """Render full YAML for the non-Bound PVCs of a namespace only.

    Args:
        namespace (str): Namespace holding the PVCs.
        probes (_Probes): Probe runner of the current snapshot.

    Returns:
        list[str]: The PVC listing record plus one record per non-Bound PVC; only the listing
            record when the listing cannot be parsed.
    """
    listing = probes.run(["oc", "-n", namespace, "get", "pvc", "-o", "json"])
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
            probes.run(["oc", "-n", namespace, "get", "pvc", name, "-o", "yaml"])
            for name in pending_names[:_MAX_PENDING_PVC_DUMPS]
        ),
    ]


def _dump_storage_scope(probes: _Probes, *, namespace: str) -> list[str]:
    """Collect PVC, DataVolume, event and ceph-csi provisioner state of a stalled migration.

    Args:
        probes (_Probes): Probe runner of the current snapshot.
        namespace (str): Migration target namespace.

    Returns:
        list[str]: One rendered command record per probe.
    """
    sections = [
        probes.run(["oc", "-n", namespace, "get", "pvc,dv,pv", "-o", "wide"]),
        *_pending_pvc_yaml(namespace=namespace, probes=probes),
        probes.run(["oc", "-n", namespace, "get", "events", "--sort-by=.lastTimestamp"]),
        probes.run(["oc", "get", "sc", "-o", "wide"]),
        probes.run(["oc", "-n", _STORAGE_NAMESPACE, "get", "pods", "-o", "wide"]),
    ]
    provisioner_logs = [
        "oc",
        "-n",
        _STORAGE_NAMESPACE,
        "logs",
        f"deploy/{_CSI_PROVISIONER_DEPLOYMENT}",
        "--all-containers",
        "--prefix",
        f"--tail={_CSI_LOG_TAIL_LINES}",
    ]
    # A Deployment can run several replicas; without --all-pods oc picks one pod and the replica that
    # logged the refused volume is silently omitted. The flag is recent, so an older client rejects
    # the whole command; the same command without it is retried and both records are kept.
    log_args = [*provisioner_logs, _ALL_PODS_FLAG]
    current_logs = probes.run(log_args)
    sections.append(current_logs)
    if not _succeeded(current_logs):
        log_args = provisioner_logs
        sections.append(f"# retried without {_ALL_PODS_FLAG}: the oc client did not accept the flag")
        sections.append(probes.run(log_args))
    # The current log always: it holds the errors produced at dump time even when the container
    # restarted. The previous container's log is added only when one exists.
    previous_logs = probes.run([*log_args, "--previous"])
    if _succeeded(previous_logs):
        sections.append(previous_logs)
    return sections


def _with_deadline_record(*, sections: list[str], probes: _Probes) -> list[str]:
    """Close a set of records with the truncation note of a snapshot cut short by its deadline.

    Args:
        sections (list[str]): Rendered records collected so far.
        probes (_Probes): Probe runner whose budget bounded the collection.

    Returns:
        list[str]: The same records, plus one summary line when probes were skipped.
    """
    deadline_record = probes.deadline_record()
    if deadline_record is not None:
        sections.append(deadline_record)
    return sections


def _write_snapshot(*, collector_path: Path, scope: str, reason: str, sections: list[str], context_note: str) -> Path:
    """Write one snapshot file.

    Args:
        collector_path (Path): Data-collector root directory.
        scope (str): Snapshot scope, used in the file name and header.
        reason (str): Failure reason, used in the file name and header.
        sections (list[str]): Rendered command records, written in order.
        context_note (str): Which kubeconfig the ``oc`` probes used, recorded in the header.

    Returns:
        Path: Path of the written snapshot.
    """
    directory = collector_path / _CLUSTER_STATE_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_path = directory / f"{utc_timestamp()}-{scope}-{_reason_slug(reason)}{_worker_suffix()}.txt"
    header = f"# reason: {reason}\n# scope: {scope}\n# oc context: {context_note}\n# collected: {utc_timestamp()}"
    snapshot_path.write_text(f"{header}\n\n{_SECTION_SEPARATOR.join(sections)}\n", encoding="utf-8")
    return snapshot_path


def _collect_and_write(
    *, reason: str, scope: str, collect: Callable[[_Probes], list[str]], kubeconfig: Path | None, context_note: str
) -> None:
    """Write one snapshot for a scope, skipping it when collection is disabled and never raising.

    Args:
        reason (str): Failure reason, used in the file name and header.
        scope (str): Snapshot scope, used in the file name and header.
        collect (Callable[[_Probes], list[str]]): Probe collector of the scope, called only when
            collection is enabled.
        kubeconfig (Path | None): Temporary kubeconfig every ``oc`` probe must use, or None for the ambient context.
        context_note (str): Which kubeconfig the probes used, recorded in the snapshot header.
    """
    collector_path = data_collector_path()
    if collector_path is None:
        return

    probes = _Probes(kubeconfig=kubeconfig)
    try:
        # A snapshot cut short by its deadline says so, so a short file is never mistaken for a
        # healthy cluster that simply had nothing to report.
        snapshot_path = _write_snapshot(
            collector_path=collector_path,
            scope=scope,
            reason=reason,
            sections=_with_deadline_record(sections=collect(probes), probes=probes),
            context_note=context_note,
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        LOGGER.warning(f"Cluster state snapshot for scope '{scope}' (reason '{reason}') failed: {exc}")
        return

    LOGGER.warning(f"Cluster state snapshot written to {snapshot_path}")


def _kubeconfig_document(configuration: Configuration) -> dict[str, Any] | None:
    """Build a kubeconfig document from the run client's configuration.

    Args:
        configuration (Configuration): Client configuration holding the API server and credentials.

    Returns:
        dict[str, Any] | None: Kubeconfig document, or None when the configuration has no server.
    """
    server = configuration.host
    if not server:
        return None

    # The document is free-form kubeconfig YAML with no static type in this repository.
    cluster: dict[str, Any] = {"server": server}
    if configuration.verify_ssl:
        if configuration.ssl_ca_cert:
            cluster["certificate-authority"] = configuration.ssl_ca_cert
    else:
        cluster["insecure-skip-tls-verify"] = True

    user: dict[str, Any] = {}
    # The client keeps the token with its scheme prefix ("Bearer <token>"); kubeconfig holds the raw one.
    token = (configuration.api_key or {}).get("authorization", "").split()
    if token:
        user["token"] = token[-1]
    if configuration.cert_file:
        user["client-certificate"] = configuration.cert_file
    if configuration.key_file:
        user["client-key"] = configuration.key_file

    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": _KUBECONFIG_CONTEXT_NAME, "cluster": cluster}],
        "users": [{"name": _KUBECONFIG_CONTEXT_NAME, "user": user}],
        "contexts": [
            {
                "name": _KUBECONFIG_CONTEXT_NAME,
                "context": {"cluster": _KUBECONFIG_CONTEXT_NAME, "user": _KUBECONFIG_CONTEXT_NAME},
            }
        ],
        "current-context": _KUBECONFIG_CONTEXT_NAME,
    }


@contextmanager
def _temporary_kubeconfig(client: DynamicClient | None) -> Iterator[tuple[Path | None, str]]:
    """Yield a throwaway kubeconfig built from the run client, deleted when the snapshot ends.

    A container run has no ambient kubeconfig, so ``oc`` in every probe would fail with "no context"
    and the snapshot would be empty - the exact scenario these dumps exist for.

    Args:
        client (DynamicClient | None): Live cluster client, or None to use the ambient oc context.

    Yields:
        tuple[Path | None, str]: Kubeconfig path passed to every probe (None for the ambient context)
            and the note recorded in the snapshot header. The token is never logged or persisted.
    """
    document = _kubeconfig_document(configuration=client.configuration) if client is not None else None
    if document is None:
        yield None, _AMBIENT_CONTEXT_NOTE
        return

    with tempfile.TemporaryDirectory(prefix=_KUBECONFIG_DIRECTORY_PREFIX, ignore_cleanup_errors=True) as directory:
        kubeconfig = Path(directory) / _KUBECONFIG_FILE_NAME
        try:
            kubeconfig.write_text(yaml.safe_dump(data=document), encoding="utf-8")
            kubeconfig.chmod(_KUBECONFIG_FILE_MODE)
        except OSError as exc:
            LOGGER.warning(
                f"Could not write the temporary kubeconfig {kubeconfig}, using the ambient oc context: {exc}"
            )
            yield None, _AMBIENT_CONTEXT_NOTE
            return
        LOGGER.info(f"Cluster state probes use the temporary kubeconfig {kubeconfig}, deleted after this snapshot")
        yield kubeconfig, f"temporary kubeconfig {kubeconfig} (deleted after this snapshot)"


def dump_cluster_state(
    *,
    reason: str,
    scope: Literal["upgrade", "storage"],
    namespace: str | None = None,
    client: DynamicClient | None = None,
) -> None:
    """Write a bounded cluster-state snapshot for a real failure.

    Called immediately before a failure is raised, so the original exception stays the error the
    caller sees: collection is best effort, every command is time-bounded, and nothing escapes.

    Args:
        reason (str): Short cause, used as the file-name slug and the file header.
        scope (Literal["upgrade", "storage"]): Which snapshot to collect.
        namespace (str | None): Migration target namespace, required by the storage scope.
        client (DynamicClient | None): Run client; when given, the probes use an ephemeral kubeconfig
            built from it instead of the ambient oc context.

    Raises:
        ValueError: If the storage scope is requested without a namespace.
    """
    collect: Callable[[_Probes], list[str]]
    if scope == "storage":
        if namespace is None:
            raise ValueError("dump_cluster_state with scope='storage' requires the migration target namespace")
        collect = partial(_dump_storage_scope, namespace=namespace)
    else:
        collect = _dump_upgrade_scope

    with _temporary_kubeconfig(client=client) as (kubeconfig, context_note):
        _collect_and_write(
            reason=reason, scope=scope, collect=collect, kubeconfig=kubeconfig, context_note=context_note
        )


def _session_start_cluster_sections(probes: _Probes) -> list[str]:
    """Collect the pre-run cluster state used as the run baseline.

    Args:
        probes (_Probes): Probe runner of the current snapshot.

    Returns:
        list[str]: One rendered command record per probe.
    """
    return [
        probes.run(["oc", "get", "clusterversion", "-o", "yaml"]),
        probes.run(["oc", "get", "co"]),
        probes.run(["oc", "get", "mcp", "-o", "yaml"]),
        _mcp_pool_summary(probes=probes),
        probes.run(["oc", "get", "nodes", "-o", "wide"]),
        probes.run(["oc", "-n", _MTV_NAMESPACE, "get", "csv"]),
        probes.run(["oc", "-n", _CNV_NAMESPACE, "get", "csv"]),
    ]


def _repository_root() -> Path:
    """Return the checkout this module is imported from.

    Returns:
        Path: Repository root candidate; it has no ``.git`` when the suite runs installed.
    """
    return Path(__file__).resolve().parents[1]


def _git_head(probes: _Probes) -> str:
    """Read the checkout's current revision.

    Args:
        probes (_Probes): Probe runner of the current snapshot.

    Returns:
        str: ``git rev-parse HEAD`` output, or ``unknown`` when the checkout has no ``.git``
            or the command fails.
    """
    repository_root = _repository_root()
    if not (repository_root / ".git").exists():
        return _GIT_HEAD_FALLBACK

    record = probes.run(["git", "-C", str(repository_root), "rev-parse", "HEAD"])
    if not _succeeded(record):
        LOGGER.warning(f"Could not read the checkout revision from {repository_root}, recording {_GIT_HEAD_FALLBACK}")
        return _GIT_HEAD_FALLBACK
    return _stdout_of(record).strip()


def _redact_invocation(pytest_args: Sequence[str]) -> str:
    """Render the pytest invocation with every credential removed.

    Args:
        pytest_args (Sequence[str]): Arguments pytest was invoked with.

    Returns:
        str: The invocation with credential ``--tc`` values and URL userinfo replaced by ``***``.
    """
    command_line = " ".join(_redact_argument(arg=arg) for arg in pytest_args)
    # The credential may come from CLUSTER_PASSWORD alone, so it can appear outside any --tc value;
    # a missing password leaves the text as is instead of aborting the whole baseline.
    return redact_secret(text=command_line, secret=resolve_cluster_credential("cluster_password") or "")


def _redact_argument(arg: str) -> str:
    """Redact the secrets one invocation argument can carry.

    Args:
        arg (str): One pytest argument, as given on the command line.

    Returns:
        str: The argument with a credential ``--tc`` value and any URL userinfo redacted.
    """
    if arg.startswith(_TC_ARGUMENT_PREFIX):
        key, separator, _ = arg[len(_TC_ARGUMENT_PREFIX) :].partition(":")
        if separator and _CREDENTIAL_KEY_PATTERN.search(key):
            arg = f"{_TC_ARGUMENT_PREFIX}{key}:{_REDACTED_VALUE}"
    return _URL_USERINFO_PATTERN.sub(rf"\g<scheme>{_REDACTED_VALUE}@", arg)


def _session_start_identity_sections(
    *, probes: _Probes, pytest_args: Sequence[str], skip_teardown: bool, skip_data_collector: bool
) -> list[str]:
    """Collect the run identity: checkout revision, pytest invocation and skip flags.

    Args:
        probes (_Probes): Probe runner of the current snapshot.
        pytest_args (Sequence[str]): Arguments pytest was invoked with, including ``--tc`` overrides.
        skip_teardown (bool): Whether ``--skip-teardown`` was set.
        skip_data_collector (bool): Whether ``--skip-data-collector`` was set.

    Returns:
        list[str]: One rendered record per identity probe.
    """
    return [
        f"$ git rev-parse HEAD\n{_git_head(probes=probes)}",
        f"$ pytest\n{_redact_invocation(pytest_args=pytest_args)}",
        probes.run(["oc", "whoami"]),
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
    temporary_path.write_text(text, encoding="utf-8")
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
        cluster_probes = _Probes()
        cluster_sections = _with_deadline_record(
            sections=_session_start_cluster_sections(probes=cluster_probes), probes=cluster_probes
        )
        cluster_created = _write_once_atomic(
            path=cluster_path,
            text=f"{_SECTION_SEPARATOR.join(cluster_sections)}\n",
        )
        identity_probes = _Probes()
        identity_sections = _session_start_identity_sections(
            probes=identity_probes,
            pytest_args=pytest_args,
            skip_teardown=skip_teardown,
            skip_data_collector=skip_data_collector,
        )
        identity_created = _write_once_atomic(
            path=identity_path,
            text=(
                f"{_SECTION_SEPARATOR.join(_with_deadline_record(sections=identity_sections, probes=identity_probes))}\n"
            ),
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        # Best-effort evidence: a failing probe skips the baseline instead of aborting session start.
        LOGGER.warning(f"Session-start baseline could not be written to {directory}: {exc}")
        return

    if cluster_created and identity_created:
        LOGGER.warning(f"Session-start baseline written to {directory}")
        return
    LOGGER.info(f"Session-start baseline already exists in {directory}, keeping the first snapshot")
