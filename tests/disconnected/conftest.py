"""Fixtures for disconnected (offline) MTV deployment tests.

Modeled on tests/upgrade/conftest.py — clones mtv-autodeploy at test
time and provides the script paths needed to run the disconnected flow.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from typing import TYPE_CHECKING

import pytest
from git import Repo
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from utilities.mtv_migration import (
    create_plan_resource,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from pathlib import Path

    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider

LOGGER = get_logger(name=__name__)


@pytest.fixture(scope="session")
def disconnected_repo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Clone mtv-autodeploy and return the clone directory.

    Reads disconnected_repo_url and disconnected_repo_ref from test config
    (passed via --tc= from Jenkins or the command line).
    """
    repo_url: str = py_config["disconnected_repo_url"]
    repo_ref: str = py_config["disconnected_repo_ref"]

    if not repo_url or not repo_ref:
        raise ValueError("disconnected_repo_url and disconnected_repo_ref must be non-empty")

    clone_dir = tmp_path_factory.mktemp("mtv-autodeploy-disconnected")

    LOGGER.info(f"Cloning {repo_url} (ref={repo_ref}) into {clone_dir}")
    if repo_url.startswith("file://"):
        # Local path — just copy (fast, preserves uncommitted changes)
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
    # Optional key: when absent, auto-detect or compute the catalog name below.
    catalog_name: str = py_config["private_catalog_name"] if "private_catalog_name" in py_config else ""

    if not catalog_name:
        result = subprocess.run(
            [
                "oc",
                "get",
                "catalogsource",
                "-n",
                "openshift-marketplace",
                "--no-headers",
                "-o",
                "custom-columns=NAME:.metadata.name",
            ],
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
            ocp_result = subprocess.run(["oc", "version"], capture_output=True, text=True, timeout=30)
            match = re.search(r"Server Version: (\d+)\.(\d+)", ocp_result.stdout)
            if match:
                major, minor = match.groups()
                catalog_name = f"cs-mtv-disconnected-redhat-redhat-operator-index-v{major}-{minor}"
                LOGGER.info(f"Computed expected private catalog name: {catalog_name}")
        except (OSError, subprocess.SubprocessError) as exc:
            # oc unavailable or timed out — log and fall through to the explicit error below
            LOGGER.warning(f"Failed to detect OCP version via 'oc version': {exc}")

    if not catalog_name:
        raise ValueError(
            "private_catalog_name must be provided via --tc=private_catalog_name:<name> "
            "or the mirror script must have been run first"
        )
    return catalog_name


@pytest.fixture(scope="class")
def storage_map_resource(
    prepared_plan: dict,
    fixture_store: dict,
    ocp_admin_client: DynamicClient,
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    target_namespace: str,
) -> StorageMap:
    """Create StorageMap resource for disconnected migration tests.

    Modeled on tests/upgrade/conftest.py::pre_upgrade_storage_map.
    """
    vms: list[str] = [vm["name"] for vm in prepared_plan["virtual_machines"]]
    return get_storage_migration_map(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        vms=vms,
    )


@pytest.fixture(scope="class")
def network_map_resource(
    prepared_plan: dict,
    fixture_store: dict,
    ocp_admin_client: DynamicClient,
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    target_namespace: str,
    multus_network_name: dict[str, str],
) -> NetworkMap:
    """Create NetworkMap resource for disconnected migration tests.

    Modeled on tests/upgrade/conftest.py::pre_upgrade_network_map.
    """
    vms: list[str] = [vm["name"] for vm in prepared_plan["virtual_machines"]]
    return get_network_migration_map(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        multus_network_name=multus_network_name,
        vms=vms,
    )


@pytest.fixture(scope="class")
def plan_resource(
    prepared_plan: dict,
    fixture_store: dict,
    ocp_admin_client: DynamicClient,
    source_provider: BaseProvider,
    destination_provider: OCPProvider,
    source_provider_inventory: ForkliftInventory,
    target_namespace: str,
    storage_map_resource: StorageMap,
    network_map_resource: NetworkMap,
) -> Plan:
    """Create MTV Plan CR resource for disconnected migration tests.

    Modeled on tests/upgrade/conftest.py::pre_upgrade_plan_resource.
    """
    populate_vm_ids(prepared_plan, source_provider_inventory)
    return create_plan_resource(
        ocp_admin_client=ocp_admin_client,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        storage_map=storage_map_resource,
        network_map=network_map_resource,
        virtual_machines_list=prepared_plan["virtual_machines"],
        target_namespace=target_namespace,
        warm_migration=prepared_plan.get("warm_migration", False),
    )
