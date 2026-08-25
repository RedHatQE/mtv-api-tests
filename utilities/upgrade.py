"""
Upgrade utilities for MTV operator upgrade tests.

This module provides functions to run the MTV upgrade process.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from exceptions.exceptions import MtvUpgradeError

LOGGER = get_logger(name=__name__)

_UPGRADE_TIMEOUT_SECONDS = 3600


def _read_upgrade_log(log_file: tempfile._TemporaryFileWrapper[bytes], password: str) -> str:
    """Read upgrade script output from the temp log and redact the cluster password.

    Args:
        log_file (tempfile._TemporaryFileWrapper[bytes]): File the upgrade script wrote to.
        password (str): Cluster password to redact from the output.

    Returns:
        str: Log text with the password replaced by ``***``.
    """
    log_file.flush()
    with open(log_file.name, encoding="utf-8", errors="replace") as readable:
        return readable.read().replace(password, "***")


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
    with tempfile.NamedTemporaryFile(prefix="mtv-upgrade-", suffix=".log") as log_file:
        try:
            subprocess.run(
                [script_path],
                env=env,
                cwd=os.path.dirname(script_path),
                check=True,
                timeout=_UPGRADE_TIMEOUT_SECONDS,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except subprocess.TimeoutExpired as exc:
            output = _read_upgrade_log(log_file, password)
            raise MtvUpgradeError(
                f"MTV upgrade script timed out after {exc.timeout} seconds\noutput: {output}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            output = _read_upgrade_log(log_file, password)
            raise MtvUpgradeError(
                f"MTV upgrade script failed with exit code {exc.returncode}\noutput: {output}"
            ) from exc

        output = _read_upgrade_log(log_file, password)

    LOGGER.info(f"MTV upgrade output:\n{output}")
    LOGGER.info(f"MTV upgrade to version {mtv_version} completed successfully")
