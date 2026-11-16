"""Fixtures for disconnected (offline) MTV deployment tests.

Modeled on tests/upgrade/conftest.py — clones mtv-autodeploy at test
time and provides the script paths needed to run the disconnected flow.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest
from git import Repo
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = get_logger(name=__name__)


@pytest.fixture(scope="session")
def disconnected_repo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Clone mtv-autodeploy and return the clone directory.

    Reads disconnected_repo_url and disconnected_repo_ref from test config
    (passed via --tc= from Jenkins or the command line).
    """
    repo_url: str = py_config.get("disconnected_repo_url", "")
    repo_ref: str = py_config.get("disconnected_repo_ref", "")

    if not repo_url:
        raise ValueError("disconnected_repo_url must be provided via --tc=disconnected_repo_url:<url>")
    if not repo_ref:
        raise ValueError("disconnected_repo_ref must be provided via --tc=disconnected_repo_ref:<ref>")

    clone_dir = tmp_path_factory.mktemp("mtv-autodeploy-disconnected")

    LOGGER.info(f"Cloning {repo_url} (ref={repo_ref}) into {clone_dir}")
    if repo_url.startswith("file://"):
        # Local path — just copy (fast, preserves uncommitted changes)
        import shutil

        local_path = repo_url.replace("file://", "")
        shutil.copytree(local_path, str(clone_dir), dirs_exist_ok=True)
    else:
        Repo.clone_from(url=repo_url, to_path=str(clone_dir), branch=repo_ref, depth=1)

    return clone_dir


@pytest.fixture(scope="session")
def disconnected_mirror_script(disconnected_repo_dir: Path) -> str:
    """Return the executable path to the mirror script."""
    script_path = disconnected_repo_dir / "disconnected" / "mirror-to-private-registry.sh"
    if not script_path.is_file():
        raise ValueError(f"Mirror script not found at '{script_path}'")
    current_mode = script_path.stat().st_mode
    script_path.chmod(current_mode | stat.S_IXUSR)
    return str(script_path)


@pytest.fixture(scope="session")
def disconnected_install_script(disconnected_repo_dir: Path) -> str:
    """Return the executable path to mtv-install.sh."""
    script_path = disconnected_repo_dir / "mtv-install.sh"
    if not script_path.is_file():
        raise ValueError(f"Install script not found at '{script_path}'")
    current_mode = script_path.stat().st_mode
    script_path.chmod(current_mode | stat.S_IXUSR)
    return str(script_path)


@pytest.fixture(scope="session")
def private_catalog_name() -> str:
    """Return the CatalogSource name for the mirrored catalog.

    If not provided via --tc=, compute the expected name from the cluster's
    OCP version (the mirror script auto-detects the same version), or
    auto-detect from existing catalogs as fallback.
    """
    catalog_name: str = py_config.get("private_catalog_name", "")

    if not catalog_name:
        import subprocess

        result = subprocess.run(
            ["oc", "get", "catalogsource", "-n", "openshift-marketplace", "--no-headers", "-o", "custom-columns=NAME:.metadata.name"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if "cs-mtv-disconnected" in line:
                    catalog_name = line.strip()
                    LOGGER.info(f"Auto-detected private catalog: {catalog_name}")
                    break

    if not catalog_name:
        # Compute the expected name from the cluster's OCP version
        # (the mirror script auto-detects the same version and creates
        # a catalog named cs-mtv-disconnected-redhat-redhat-operator-index-v<X-Y>)
        try:
            ocp_result = subprocess.run(
                ["oc", "version"], capture_output=True, text=True, timeout=30
            )
            import re

            match = re.search(r"Server Version: (\d+)\.(\d+)", ocp_result.stdout)
            if match:
                major, minor = match.groups()
                catalog_name = f"cs-mtv-disconnected-redhat-redhat-operator-index-v{major}-{minor}"
                LOGGER.info(f"Computed expected private catalog name: {catalog_name}")
        except Exception:
            pass

    if not catalog_name:
        raise ValueError(
            "private_catalog_name must be provided via --tc=private_catalog_name:<name> "
            "or the mirror script must have been run first"
        )
    return catalog_name
