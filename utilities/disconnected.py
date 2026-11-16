"""
Disconnected (offline) deployment utilities for MTV.

Provides functions to run the MTV disconnected deployment scripts
(mirror + install) as subprocesses. Subprocess handling is delegated
to utilities/script_runner.py (shared with utilities/upgrade.py).
"""

from __future__ import annotations

import os

from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from utilities.script_runner import run_script

LOGGER = get_logger(name=__name__)

_DISCONNECTED_TIMEOUT_SECONDS = 3600


def _build_base_env() -> dict[str, str]:
    """Build the common environment for disconnected deployment scripts."""
    env = os.environ.copy()
    env.update({
        "CLUSTER_USERNAME": py_config["cluster_username"],
        "CLUSTER_PASSWORD": py_config["cluster_password"],
        "CLUSTER_API_URL": py_config["cluster_host"],
        "DEBUG": "true",
    })
    return env


def run_disconnected_mirror(
    script_path: str,
    mtv_version: str,
    private_registry: str,
    **kwargs: str,
) -> None:
    """Run the disconnected mirror script (mirror GA content to private registry).

    Args:
        script_path (str): Full path to mirror-to-private-registry.sh.
        mtv_version (str): MTV version to mirror (e.g. "2.12.10").
        private_registry (str): Destination registry (e.g. "quay.io/org/mtv-disconnected").
        **kwargs (str): Additional environment variables passed to the script
            (e.g. OCP_INDEX_VERSION, MAX_NESTED_PATHS).
    """
    channel_version = ".".join(mtv_version.split(".")[:2])
    env = _build_base_env()
    env.update({
        "MTV_VERSION": mtv_version,
        "MTV_CHANNEL_VERSION": channel_version,
        "PRIVATE_REGISTRY": private_registry,
        "MODE": "full",
        **kwargs,
    })

    run_script(
        script_path=script_path,
        env=env,
        password=env["CLUSTER_PASSWORD"],
        description="Disconnected mirror script",
        timeout_seconds=_DISCONNECTED_TIMEOUT_SECONDS,
    )


def run_disconnected_install(
    script_path: str,
    mtv_version: str,
    catalog_name: str,
) -> None:
    """Run mtv-install.sh with MTV_SOURCE=PRIVATE_REGISTRY.

    Args:
        script_path (str): Full path to mtv-install.sh.
        mtv_version (str): MTV version to install (e.g. "2.12.10").
        catalog_name (str): The CatalogSource name from the mirror step.
    """
    env = _build_base_env()
    env.update({
        "MTV_VERSION": mtv_version,
        "MTV_SOURCE": "PRIVATE_REGISTRY",
        "PRIVATE_CATALOG_NAME": catalog_name,
    })

    run_script(
        script_path=script_path,
        env=env,
        password=env["CLUSTER_PASSWORD"],
        description="Disconnected install script",
        timeout_seconds=_DISCONNECTED_TIMEOUT_SECONDS,
    )
