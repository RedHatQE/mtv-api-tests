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


def _kill_process_group(pgid: int) -> None:
    """Terminate leftover children in the upgrade script process group.

    Args:
        pgid (int): Process group id (the upgrade script pid when using start_new_session).
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def _run_upgrade_script(
    script_path: str,
    env: dict[str, str],
    stdout_file: IO[bytes],
    stderr_file: IO[bytes],
) -> int:
    """Run the upgrade script in its own session and reap leftover children.

    Args:
        script_path (str): Full path to the upgrade script.
        env (dict[str, str]): Environment for the script.
        stdout_file (IO[bytes]): Destination for stdout.
        stderr_file (IO[bytes]): Destination for stderr.

    Returns:
        int: Process exit code.

    Raises:
        subprocess.TimeoutExpired: If the script does not exit within the timeout.
    """
    proc = subprocess.Popen(
        [script_path],
        env=env,
        cwd=os.path.dirname(script_path),
        start_new_session=True,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    try:
        return proc.wait(timeout=_UPGRADE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc.pid)
        proc.wait()
        raise
    finally:
        _kill_process_group(proc.pid)


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
        try:
            returncode = _run_upgrade_script(
                script_path=script_path,
                env=env,
                stdout_file=stdout_file,
                stderr_file=stderr_file,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _read_upgrade_log(stdout_file, password)
            stderr = _read_upgrade_log(stderr_file, password)
            raise MtvUpgradeError(
                f"MTV upgrade script timed out after {exc.timeout} seconds\nstdout: {stdout}\nstderr: {stderr}"
            ) from exc

        stdout = _read_upgrade_log(stdout_file, password)
        stderr = _read_upgrade_log(stderr_file, password)
        if returncode != 0:
            raise MtvUpgradeError(
                f"MTV upgrade script failed with exit code {returncode}\nstdout: {stdout}\nstderr: {stderr}"
            )

    LOGGER.info(f"MTV upgrade stdout:\n{stdout}")
    if stderr:
        LOGGER.warning(f"MTV upgrade stderr:\n{stderr}")
    LOGGER.info(f"MTV upgrade to version {mtv_version} completed successfully")
