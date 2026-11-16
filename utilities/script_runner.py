"""
Shared subprocess runner for MTV deployment scripts.

Provides a common interface for running bash scripts (upgrade, disconnected
mirror, disconnected install) as subprocesses with proper process-group
isolation, file-redirected logging, password redaction, timeout enforcement,
and cleanup.

Extracted from utilities/upgrade.py so that utilities/disconnected.py and
utilities/upgrade.py can share the same subprocess handling (DRY).
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from typing import IO

from simple_logger.logger import get_logger

from exceptions.exceptions import MtvUpgradeError

LOGGER = get_logger(name=__name__)

_PROCESS_GROUP_TERM_TIMEOUT_SECONDS = 15
_PROCESS_GROUP_KILL_TIMEOUT_SECONDS = 15
_PROCESS_GROUP_CLEANUP_TIMEOUT_SECONDS = (
    _PROCESS_GROUP_TERM_TIMEOUT_SECONDS + _PROCESS_GROUP_KILL_TIMEOUT_SECONDS
)


def read_log(log_file: IO[bytes], password: str) -> str:
    """Read script output from an open temp log and redact the cluster password.

    Args:
        log_file (IO[bytes]): Open file the script wrote to.
        password (str): Cluster password to redact from the output.

    Returns:
        str: Log text with the password replaced by ``***``.
    """
    log_file.flush()
    log_file.seek(0)
    raw = log_file.read()
    return raw.decode("utf-8", errors="replace").replace(password, "***")


def _kill_process_group(pgid: int, sig: int) -> None:
    """Send a signal to leftover children in the script process group.

    Args:
        pgid (int): Process group id (the script pid when using start_new_session).
        sig (int): Signal to send (SIGTERM or SIGKILL).
    """
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def stop_process(proc: subprocess.Popen[bytes]) -> None:
    """Stop the script process group with bounded SIGTERM then SIGKILL waits.

    Args:
        proc (subprocess.Popen[bytes]): The script process (session leader).

    Raises:
        MtvUpgradeError: If the process is still running after SIGKILL.
    """
    _kill_process_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=_PROCESS_GROUP_TERM_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        LOGGER.warning(f"Script process group {proc.pid} did not exit after SIGTERM, sending SIGKILL")
    _kill_process_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=_PROCESS_GROUP_KILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise MtvUpgradeError(
            f"Script process group {proc.pid} did not exit after SIGTERM and SIGKILL"
        ) from exc


def run_script(
    script_path: str,
    env: dict[str, str],
    password: str,
    description: str,
    timeout_seconds: int = 3600,
) -> None:
    """Run a deployment script with proper subprocess hygiene.

    Uses file-redirected logs (not PIPEs) because leftover script children
    would keep communicate() blocked. Runs in its own process group
    (start_new_session=True) so that SIGTERM/SIGKILL cleanup can reach
    all children on timeout or failure.

    Args:
        script_path (str): Full path to the script to run.
        env (dict[str, str]): Full environment for the script (must include
            CLUSTER_PASSWORD — used for log redaction).
        password (str): Cluster password to redact from logs.
        description (str): Human-readable description for logging and error messages.
        timeout_seconds (int): Maximum time to wait for the script to complete.

    Raises:
        MtvUpgradeError: If the script exits with a non-zero status or times out.
    """
    LOGGER.info(f"Running {description}: {script_path}")

    with (
        tempfile.NamedTemporaryFile(prefix="mtv-script-stdout-", suffix=".log") as stdout_file,
        tempfile.NamedTemporaryFile(prefix="mtv-script-stderr-", suffix=".log") as stderr_file,
    ):
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
            returncode = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            timeout_exc = exc
            returncode = None
        finally:
            try:
                stop_process(proc)
            except MtvUpgradeError as exc:
                cleanup_exc = exc

        stdout = read_log(stdout_file, password)
        stderr = read_log(stderr_file, password)
        cleanup_note = f"\ncleanup: {cleanup_exc}" if cleanup_exc is not None else ""
        if timeout_exc is not None:
            raise MtvUpgradeError(
                f"{description} timed out after {timeout_exc.timeout} seconds "
                f"(plus up to {_PROCESS_GROUP_CLEANUP_TIMEOUT_SECONDS} seconds process-group cleanup)\n"
                f"stdout: {stdout}\nstderr: {stderr}{cleanup_note}"
            ) from timeout_exc
        if returncode is None or returncode != 0:
            raise MtvUpgradeError(
                f"{description} failed with exit code {returncode}\nstdout: {stdout}\nstderr: {stderr}{cleanup_note}"
            ) from cleanup_exc
        if cleanup_exc is not None:
            raise cleanup_exc

    LOGGER.info(f"{description} stdout:\n{stdout}")
    if stderr:
        LOGGER.warning(f"{description} stderr:\n{stderr}")
    LOGGER.info(f"{description} completed successfully")
