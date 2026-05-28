from __future__ import annotations

import stat
from typing import TYPE_CHECKING, Any

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
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider

LOGGER = get_logger(name=__name__)


@pytest.fixture(scope="session")
def upgrade_script_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Clone the mtv-autodeploy repo and return the absolute path to the upgrade script.

    Args:
        tmp_path_factory (pytest.TempPathFactory): Pytest factory for session-scoped temporary directories.

    Returns:
        str: Absolute path to the executable upgrade script.

    Raises:
        ValueError: If required config values are missing or the script is not found after cloning.
        git.exc.GitCommandError: If the git clone fails.
    """
    repo_url: str = py_config.get("upgrade_repo_url", "")
    repo_ref: str = py_config.get("upgrade_repo_ref", "")
    script_relative_path: str = py_config.get("upgrade_script_path", "")

    if not repo_url:
        raise ValueError("upgrade_repo_url must be provided via --tc=upgrade_repo_url:<url>")
    if not repo_ref:
        raise ValueError("upgrade_repo_ref must be provided via --tc=upgrade_repo_ref:<ref>")
    if not script_relative_path:
        raise ValueError("upgrade_script_path must be provided via --tc=upgrade_script_path:<relative-path>")

    clone_dir = tmp_path_factory.mktemp("mtv-autodeploy")

    LOGGER.info(f"Cloning {repo_url} (ref={repo_ref}) into {clone_dir}")
    Repo.clone_from(url=repo_url, to_path=str(clone_dir), branch=repo_ref, depth=1)

    script_path = clone_dir / script_relative_path
    if not script_path.is_file():
        raise ValueError(f"Upgrade script not found at '{script_path}' after cloning {repo_url}")

    current_mode = script_path.stat().st_mode
    script_path.chmod(current_mode | stat.S_IXUSR)

    return str(script_path)


@pytest.fixture(scope="class")
def pre_upgrade_storage_map(
    prepared_plan: dict[str, Any],
    fixture_store: dict[str, Any],
    ocp_admin_client: DynamicClient,
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    target_namespace: str,
) -> StorageMap:
    """Create StorageMap resource for upgrade migration tests.

    Args:
        prepared_plan (dict[str, Any]): Test plan configuration with VM details.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        source_provider (BaseProvider): Source provider connection.
        destination_provider (BaseProvider): Destination provider.
        source_provider_inventory (ForkliftInventory): Provider inventory.
        target_namespace (str): Target namespace.

    Returns:
        StorageMap: The created StorageMap resource.
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
def pre_upgrade_network_map(
    prepared_plan: dict[str, Any],
    fixture_store: dict[str, Any],
    ocp_admin_client: DynamicClient,
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    target_namespace: str,
    multus_network_name: dict[str, str],
) -> NetworkMap:
    """Create NetworkMap resource for upgrade migration tests.

    Args:
        prepared_plan (dict[str, Any]): Test plan configuration with VM details.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        source_provider (BaseProvider): Source provider connection.
        destination_provider (BaseProvider): Destination provider.
        source_provider_inventory (ForkliftInventory): Provider inventory.
        target_namespace (str): Target namespace.
        multus_network_name (dict[str, str]): Multus network name mapping.

    Returns:
        NetworkMap: The created NetworkMap resource.
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
def pre_upgrade_plan_resource(
    prepared_plan: dict[str, Any],
    fixture_store: dict[str, Any],
    ocp_admin_client: DynamicClient,
    source_provider: BaseProvider,
    destination_provider: OCPProvider,
    source_provider_inventory: ForkliftInventory,
    target_namespace: str,
    pre_upgrade_storage_map: StorageMap,
    pre_upgrade_network_map: NetworkMap,
) -> Plan:
    """Create MTV Plan CR resource for upgrade migration tests.

    Args:
        prepared_plan (dict[str, Any]): Test plan configuration with VM details.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        source_provider (BaseProvider): Source provider connection.
        destination_provider (OCPProvider): Destination provider.
        source_provider_inventory (ForkliftInventory): Provider inventory.
        target_namespace (str): Target namespace.
        pre_upgrade_storage_map (StorageMap): Storage map for the migration.
        pre_upgrade_network_map (NetworkMap): Network map for the migration.

    Returns:
        Plan: The created Plan CR resource.
    """
    populate_vm_ids(prepared_plan, source_provider_inventory)
    return create_plan_resource(
        ocp_admin_client=ocp_admin_client,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        storage_map=pre_upgrade_storage_map,
        network_map=pre_upgrade_network_map,
        virtual_machines_list=prepared_plan["virtual_machines"],
        target_namespace=target_namespace,
        warm_migration=prepared_plan.get("warm_migration", False),
    )
