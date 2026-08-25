"""
Upgrade utilities for MTV operator upgrade tests.

This module provides functions to run the MTV upgrade process.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from typing import IO

from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from exceptions.exceptions import MtvUpgradeError

LOGGER = get_logger(name=__name__)

_UPGRADE_TIMEOUT_SECONDS = 3600
_PROCESS_GROUP_TERM_TIMEOUT_SECONDS = 15
_PROCESS_GROUP_KILL_TIMEOUT_SECONDS = 15
_PROCESS_GROUP_CLEANUP_TIMEOUT_SECONDS = _PROCESS_GROUP_TERM_TIMEOUT_SECONDS + _PROCESS_GROUP_KILL_TIMEOUT_SECONDS


def _read_upgrade_log(log_file: IO[bytes], password: str) -> str:
    """Read upgrade script output from an open temp log and redact the cluster password.

    Args:
        log_file (IO[bytes]): Open file the upgrade script wrote to.
        password (str): Cluster password to redact from the output.

    Returns:
        str: Log text with the password replaced by ``***``.
    """
    log_file.flush()
    log_file.seek(0)
    raw = log_file.read()
    return raw.decode("utf-8", errors="replace").replace(password, "***")


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
) -> None:
    """Run the MTV operator upgrade using the specified upgrade script.

    Args:
        script_path (str): Full path to the upgrade script.
        mtv_version (str): Target MTV version to upgrade to.
        mtv_source (str): MTV source identifier (e.g., "brew", "released").
        image_index (str): Optional image index override for the upgrade.

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

    # File redirect (not PIPEs): leftover mtv-upgrade.sh children would keep communicate() blocked.
    with (
        tempfile.NamedTemporaryFile(prefix="mtv-upgrade-stdout-", suffix=".log") as stdout_file,
        tempfile.NamedTemporaryFile(prefix="mtv-upgrade-stderr-", suffix=".log") as stderr_file,
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
            returncode = proc.wait(timeout=_UPGRADE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            timeout_exc = exc
            returncode = None
        finally:
            try:
                _stop_upgrade_process(proc)
            except MtvUpgradeError as exc:
                cleanup_exc = exc

        stdout = _read_upgrade_log(stdout_file, password)
        stderr = _read_upgrade_log(stderr_file, password)
        cleanup_note = f"\ncleanup: {cleanup_exc}" if cleanup_exc is not None else ""
        if timeout_exc is not None:
            raise MtvUpgradeError(
                f"MTV upgrade script timed out after {timeout_exc.timeout} seconds "
                f"(plus up to {_PROCESS_GROUP_CLEANUP_TIMEOUT_SECONDS} seconds process-group cleanup)\n"
                f"stdout: {stdout}\nstderr: {stderr}{cleanup_note}"
            ) from timeout_exc
        if returncode is None or returncode != 0:
            raise MtvUpgradeError(
                f"MTV upgrade script failed with exit code {returncode}\nstdout: {stdout}\nstderr: {stderr}{cleanup_note}"
            ) from cleanup_exc
        if cleanup_exc is not None:
            raise cleanup_exc

    LOGGER.info(f"MTV upgrade stdout:\n{stdout}")
    if stderr:
        LOGGER.warning(f"MTV upgrade stderr:\n{stderr}")
    LOGGER.info(f"MTV upgrade to version {mtv_version} completed successfully")
