"""
Upgrade utilities for MTV operator upgrade tests.

This module provides functions to run the MTV upgrade process.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from simple_logger.logger import get_logger

from exceptions.exceptions import MtvUpgradeError
from utilities.cluster_state import dump_cluster_state
from utilities.data_collector import data_collector_path, redact_secret, utc_timestamp
from utilities.utils import require_cluster_credential

LOGGER = get_logger(name=__name__)

_UPGRADE_TIMEOUT_ENV_VAR = "MTV_UPGRADE_TIMEOUT_SECONDS"
# Larger than the upgrade script's own internal waits so the script reports its own timeout and
# diagnostics instead of being SIGTERMed by this outer guard.
_UPGRADE_TIMEOUT_DEFAULT_SECONDS = 5400
_PROCESS_GROUP_TERM_TIMEOUT_SECONDS = 15
_PROCESS_GROUP_KILL_TIMEOUT_SECONDS = 15
_PROCESS_GROUP_CLEANUP_TIMEOUT_SECONDS = _PROCESS_GROUP_TERM_TIMEOUT_SECONDS + _PROCESS_GROUP_KILL_TIMEOUT_SECONDS
_UPGRADE_ARTIFACT_DIR_PREFIX = "upgrade-script"
_UPGRADE_STDOUT_LOG_NAME = "upgrade-script-stdout.log"
_UPGRADE_STDERR_LOG_NAME = "upgrade-script-stderr.log"
_UPGRADE_ERROR_LOG_TAIL_LINES = 200
_CLUSTER_PASSWORD_ENV_VAR = "CLUSTER_PASSWORD"  # pragma: allowlist secret


def resolve_upgrade_timeout(cli_value: int | None) -> int:
    """Resolve the outer upgrade wait budget from the CLI option, the environment and the default.

    Args:
        cli_value (int | None): Value of ``--mtv-upgrade-timeout``, or None when it was not given.

    Returns:
        int: Budget in seconds for the outer upgrade script wait.

    Raises:
        ValueError: If the resolved value is not a positive integer.
    """
    if cli_value is not None:
        source, raw_value = "--mtv-upgrade-timeout", str(cli_value)
    else:
        source, raw_value = _UPGRADE_TIMEOUT_ENV_VAR, os.environ.get(_UPGRADE_TIMEOUT_ENV_VAR, "")

    if not raw_value:
        return _UPGRADE_TIMEOUT_DEFAULT_SECONDS

    invalid_message = f"{source} must be a positive integer number of seconds, got '{raw_value}'"
    try:
        timeout = int(raw_value)
    except ValueError as exc:
        raise ValueError(invalid_message) from exc
    if timeout <= 0:
        raise ValueError(invalid_message)
    return timeout


def _read_upgrade_log(log_path: Path, password: str) -> str:
    """Read an upgrade script log and redact the cluster password.

    Args:
        log_path (Path): File the upgrade script wrote to.
        password (str): Cluster password to redact from the output.

    Returns:
        str: Log text with the password replaced by ``***``.
    """
    return redact_secret(text=log_path.read_bytes().decode("utf-8", errors="replace"), secret=password)


def _log_tail(log_text: str) -> str:
    """Return the last lines of a log.

    Args:
        log_text (str): Full redacted log text.

    Returns:
        str: The last ``_UPGRADE_ERROR_LOG_TAIL_LINES`` lines.
    """
    return "\n".join(log_text.splitlines()[-_UPGRADE_ERROR_LOG_TAIL_LINES:])


def _prepare_upgrade_artifact_dir() -> Path | None:
    """Create the directory that persists this run's upgrade script logs.

    Returns:
        Path | None: Created artifact directory, or None when data collection is disabled.
    """
    collector_path = data_collector_path()
    if collector_path is None:
        return None

    artifact_dir = collector_path / f"{_UPGRADE_ARTIFACT_DIR_PREFIX}-{utc_timestamp()}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _write_redacted_logs(*, artifact_dir: Path, stdout: str, stderr: str) -> tuple[Path, ...]:
    """Persist the redacted script output to the artifact directory.

    The raw child output never reaches this directory: it is read from the ephemeral copy and
    redacted first, so the cluster password cannot travel with the copied artifact tree.

    Args:
        artifact_dir (Path): Artifact directory of this run.
        stdout (str): Redacted upgrade script stdout.
        stderr (str): Redacted upgrade script stderr.

    Returns:
        tuple[Path, ...]: The log files that were actually written. Empty when the collector refused
            the write, so the failure message never points at files that do not exist.
    """
    written: list[Path] = []
    for name, content in ((_UPGRADE_STDOUT_LOG_NAME, stdout), (_UPGRADE_STDERR_LOG_NAME, stderr)):
        path = artifact_dir / name
        try:
            path.write_text(content)
        except OSError as exc:
            # A full or read-only collector must never mask the upgrade failure this artifact
            # describes; the tails are still in the raised error, so losing a file only costs the
            # full stream - but the message must not claim a file that was not written.
            LOGGER.warning(f"Could not persist the upgrade script log '{path}': {exc}")
            continue
        written.append(path)
    return tuple(written)


def _upgrade_failure_details(
    *, stdout: str, stderr: str, artifact_dir: Path | None, persisted_logs: tuple[Path, ...] = ()
) -> str:
    """Render the tail of both script logs and, when persisted, the log paths.

    Args:
        stdout (str): Redacted upgrade script stdout.
        stderr (str): Redacted upgrade script stderr.
        artifact_dir (Path | None): Directory intended to hold the redacted logs, or None when
            collection was disabled - the tails are still rendered, they are simply not persisted.
        persisted_logs (tuple[Path, ...]): Files actually written by _write_redacted_logs().

    Returns:
        str: Message fragment carrying the last lines of both streams.
    """
    if artifact_dir is None:
        persisted_note = "\n(not persisted: --skip-data-collector)"
    elif persisted_logs:
        persisted_note = f"\npersisted logs: {' '.join(str(path) for path in persisted_logs)}"
    else:
        persisted_note = f"\n(not persisted: the collector at '{artifact_dir}' rejected the write)"
    return (
        f"stdout tail (last {_UPGRADE_ERROR_LOG_TAIL_LINES} lines):\n{_log_tail(stdout)}\n"
        f"stderr tail (last {_UPGRADE_ERROR_LOG_TAIL_LINES} lines):\n{_log_tail(stderr)}"
        f"{persisted_note}"
    )


def _upgrade_script_env(*, mtv_version: str, mtv_source: str, image_index: str) -> dict[str, str]:
    """Resolve the cluster credentials and build the upgrade script environment.

    Every cluster credential is resolved the same way (pytest config first, then the
    ``CLUSTER_*`` environment variable); the CLI passes the username and password only through
    the environment, so reading pytest config directly would KeyError before the script starts.

    Args:
        mtv_version (str): Target MTV version to upgrade to.
        mtv_source (str): MTV source identifier (e.g., "brew", "released").
        image_index (str): Optional image index override for the upgrade.

    Returns:
        dict[str, str]: Environment for the upgrade script process.

    Raises:
        ValueError: If ``cluster_password``, ``cluster_username`` or ``cluster_host`` is missing or
            blank in both pytest config and the environment.
    """
    env = os.environ.copy()
    env.update({
        "MTV_VERSION": mtv_version,
        "MTV_SOURCE": mtv_source.upper(),
        "IMAGE_INDEX": image_index,
        "CLUSTER_USERNAME": require_cluster_credential("cluster_username"),
        _CLUSTER_PASSWORD_ENV_VAR: require_cluster_credential("cluster_password"),
        "CLUSTER_API_URL": require_cluster_credential("cluster_host"),
        "DEBUG": "true",
    })
    return env


def _kill_process_group(pgid: int, sig: int) -> None:
    """Send a signal to leftover children in the upgrade script process group.

    Args:
        pgid (int): Process group id (the upgrade script pid when using start_new_session).
        sig (int): Signal to send (SIGTERM or SIGKILL).
    """
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def _stop_upgrade_process(proc: subprocess.Popen[bytes]) -> None:
    """Stop the upgrade process group with bounded SIGTERM then SIGKILL waits.

    Args:
        proc (subprocess.Popen[bytes]): The upgrade script process (session leader).

    Raises:
        MtvUpgradeError: If the process is still running after SIGKILL.
    """
    _kill_process_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=_PROCESS_GROUP_TERM_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        LOGGER.warning(f"Upgrade process group {proc.pid} did not exit after SIGTERM, sending SIGKILL")
    _kill_process_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=_PROCESS_GROUP_KILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise MtvUpgradeError(f"Upgrade process group {proc.pid} did not exit after SIGTERM and SIGKILL") from exc


def _supervise_upgrade_process(
    script_path: str,
    env: dict[str, str],
    timeout: int,
    artifact_dir: Path | None,
) -> tuple[int | None, subprocess.TimeoutExpired | None, MtvUpgradeError | None, str, str, tuple[Path, ...]]:
    """Run the upgrade script and supervise its process group.

    The child always writes into an ephemeral directory, never the inherited streams: the script
    receives CLUSTER_PASSWORD, and pytest runs with ``-s``, so anything printed would otherwise land
    unredacted in the CI log. Only the redacted text is ever persisted, and only when data collection
    is enabled - with ``--skip-data-collector`` nothing is written to the artifact tree at all. File
    redirect (not PIPEs): leftover mtv-upgrade.sh children would keep communicate() blocked.

    Args:
        script_path (str): Full path to the upgrade script.
        env (dict[str, str]): Environment for the upgrade script, including the credentials.
        timeout (int): Outer wait budget in seconds.
        artifact_dir (Path | None): Directory that persists the redacted logs, or None when data
            collection is disabled.

    Returns:
        tuple[int | None, subprocess.TimeoutExpired | None, MtvUpgradeError | None, str, str, tuple[Path, ...]]: Exit
            code (None on timeout), the timeout exception, a process-group cleanup failure, and the
            redacted stdout and stderr. The redacted tails are always returned; they are persisted to
            the artifact tree only when collection is enabled.

    Raises:
        MtvUpgradeError: If the process group is still running after SIGKILL.
        OSError: If the ephemeral log files cannot be created.
    """
    password = env[_CLUSTER_PASSWORD_ENV_VAR]
    with contextlib.ExitStack() as stack:
        log_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="mtv-upgrade-logs-")))
        stdout_file = stack.enter_context((log_dir / _UPGRADE_STDOUT_LOG_NAME).open("wb"))
        stderr_file = stack.enter_context((log_dir / _UPGRADE_STDERR_LOG_NAME).open("wb"))
        proc = subprocess.Popen(
            [script_path],
            env=env,
            cwd=os.path.dirname(script_path),
            start_new_session=True,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        timeout_exc: subprocess.TimeoutExpired | None = None
        cleanup_exc: MtvUpgradeError | None = None
        stdout = ""
        stderr = ""
        persisted_logs: tuple[Path, ...] = ()
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timeout_exc = exc
            returncode = None
        finally:
            try:
                _stop_upgrade_process(proc)
            except MtvUpgradeError as exc:
                cleanup_exc = exc
            # Persist the redacted copy here (not after the try) so an unexpected wait failure still
            # leaves a redacted artifact, and the raw bytes stay in the ephemeral directory only.
            stdout = _read_upgrade_log(log_path=log_dir / _UPGRADE_STDOUT_LOG_NAME, password=password)
            stderr = _read_upgrade_log(log_path=log_dir / _UPGRADE_STDERR_LOG_NAME, password=password)
            if artifact_dir is not None:
                persisted_logs = _write_redacted_logs(artifact_dir=artifact_dir, stdout=stdout, stderr=stderr)

    return returncode, timeout_exc, cleanup_exc, stdout, stderr, persisted_logs


def run_mtv_upgrade(
    script_path: str,
    mtv_version: str,
    mtv_source: str,
    image_index: str = "",
    *,
    timeout: int,
) -> None:
    """Run the MTV operator upgrade using the specified upgrade script.

    Args:
        script_path (str): Full path to the upgrade script.
        mtv_version (str): Target MTV version to upgrade to.
        mtv_source (str): MTV source identifier (e.g., "brew", "released").
        image_index (str): Optional image index override for the upgrade.
        timeout (int): Outer wait budget in seconds. The script owns its own internal waits; this
            only guards against a wedged process.

    Raises:
        ValueError: If ``CLUSTER_PASSWORD``, ``CLUSTER_USERNAME`` or ``CLUSTER_HOST`` is missing or
            empty.
        MtvUpgradeError: If the upgrade script exits with a non-zero status or times out.
    """
    env = _upgrade_script_env(mtv_version=mtv_version, mtv_source=mtv_source, image_index=image_index)

    LOGGER.info(f"Running MTV upgrade: {script_path} (version={mtv_version}, source={mtv_source}, index={image_index})")
    LOGGER.info(
        f"MTV upgrade outer wait budget: {timeout} seconds. The upgrade script owns its internal waits; "
        "this budget only guards against a wedged process"
    )

    artifact_dir = _prepare_upgrade_artifact_dir()
    returncode, timeout_exc, cleanup_exc, stdout, stderr, persisted_logs = _supervise_upgrade_process(
        script_path=script_path, env=env, timeout=timeout, artifact_dir=artifact_dir
    )

    failure_details = _upgrade_failure_details(
        stdout=stdout, stderr=stderr, artifact_dir=artifact_dir, persisted_logs=persisted_logs
    )
    cleanup_note = f"\ncleanup: {cleanup_exc}" if cleanup_exc is not None else ""
    if timeout_exc is not None:
        dump_cluster_state(reason=f"upgrade-script-timeout-{timeout}", scope="upgrade")
        raise MtvUpgradeError(
            f"MTV upgrade script timed out after {timeout_exc.timeout} seconds "
            f"(plus up to {_PROCESS_GROUP_CLEANUP_TIMEOUT_SECONDS} seconds process-group cleanup)\n"
            f"{failure_details}{cleanup_note}"
        ) from timeout_exc
    if returncode is None or returncode != 0:
        # The script reports its own 60-minute wait first and exits non-zero, so this branch
        # (not only the outer timeout) needs the snapshot for MCP/CO evidence.
        dump_cluster_state(reason=f"upgrade-script-exit-{returncode}", scope="upgrade")
        raise MtvUpgradeError(
            f"MTV upgrade script failed with exit code {returncode}\n{failure_details}{cleanup_note}"
        ) from cleanup_exc
    if cleanup_exc is not None:
        raise cleanup_exc

    if artifact_dir is not None:
        LOGGER.info(f"MTV upgrade stdout:\n{stdout}")
        if stderr:
            LOGGER.warning(f"MTV upgrade stderr:\n{stderr}")

    LOGGER.info(f"MTV upgrade to version {mtv_version} completed successfully")
