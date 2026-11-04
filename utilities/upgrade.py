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

from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from exceptions.exceptions import MtvUpgradeError
from utilities.cluster_state import dump_cluster_state
from utilities.data_collector import data_collector_path, redact_secret, utc_timestamp

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


def _upgrade_failure_details(*, stdout: str, stderr: str, artifact_dir: Path | None) -> str:
    """Render the tail of both script logs and, when persisted, the log paths.

    Args:
        stdout (str): Redacted upgrade script stdout.
        stderr (str): Redacted upgrade script stderr.
        artifact_dir (Path | None): Persisted artifact directory, or None when logs are temporary.

    Returns:
        str: Message fragment carrying the last lines of both streams.
    """
    persisted_note = (
        f"\npersisted logs: {artifact_dir / _UPGRADE_STDOUT_LOG_NAME} {artifact_dir / _UPGRADE_STDERR_LOG_NAME}"
        if artifact_dir is not None
        else ""
    )
    return (
        f"stdout tail (last {_UPGRADE_ERROR_LOG_TAIL_LINES} lines):\n{_log_tail(stdout)}\n"
        f"stderr tail (last {_UPGRADE_ERROR_LOG_TAIL_LINES} lines):\n{_log_tail(stderr)}"
        f"{persisted_note}"
    )


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
        MtvUpgradeError: If the upgrade script exits with a non-zero status or times out.
    """
    env = os.environ.copy()
    env.update({
        "MTV_VERSION": mtv_version,
        "MTV_SOURCE": mtv_source.upper(),
        "IMAGE_INDEX": image_index,
        "CLUSTER_USERNAME": py_config["cluster_username"],
        "CLUSTER_PASSWORD": py_config["cluster_password"],
        "CLUSTER_API_URL": py_config["cluster_host"],
        "DEBUG": "true",
    })
    password = env["CLUSTER_PASSWORD"]

    LOGGER.info(f"Running MTV upgrade: {script_path} (version={mtv_version}, source={mtv_source}, index={image_index})")
    LOGGER.info(
        f"MTV upgrade outer wait budget: {timeout} seconds. The upgrade script owns its internal waits; "
        "this budget only guards against a wedged process"
    )

    artifact_dir = _prepare_upgrade_artifact_dir()

    # File redirect (not PIPEs): leftover mtv-upgrade.sh children would keep communicate() blocked.
    with contextlib.ExitStack() as stack:
        log_dir = artifact_dir or Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="mtv-upgrade-logs-")))
        stdout_path = log_dir / _UPGRADE_STDOUT_LOG_NAME
        stderr_path = log_dir / _UPGRADE_STDERR_LOG_NAME
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
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

        stdout = _read_upgrade_log(log_path=stdout_path, password=password)
        stderr = _read_upgrade_log(log_path=stderr_path, password=password)
        failure_details = _upgrade_failure_details(stdout=stdout, stderr=stderr, artifact_dir=artifact_dir)
        cleanup_note = f"\ncleanup: {cleanup_exc}" if cleanup_exc is not None else ""
        if timeout_exc is not None:
            dump_cluster_state(reason=f"upgrade-script-timeout-{timeout}", scope="upgrade")
            raise MtvUpgradeError(
                f"MTV upgrade script timed out after {timeout_exc.timeout} seconds "
                f"(plus up to {_PROCESS_GROUP_CLEANUP_TIMEOUT_SECONDS} seconds process-group cleanup)\n"
                f"{failure_details}{cleanup_note}"
            ) from timeout_exc
        if returncode is None or returncode != 0:
            raise MtvUpgradeError(
                f"MTV upgrade script failed with exit code {returncode}\n{failure_details}{cleanup_note}"
            ) from cleanup_exc
        if cleanup_exc is not None:
            raise cleanup_exc

    LOGGER.info(f"MTV upgrade stdout:\n{stdout}")
    if stderr:
        LOGGER.warning(f"MTV upgrade stderr:\n{stderr}")
    LOGGER.info(f"MTV upgrade to version {mtv_version} completed successfully")
