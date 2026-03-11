from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_cold_migration_comprehensive"])],
    indirect=True,
    ids=["comprehensive-cold"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
@pytest.mark.tier0
class TestColdMigrationComprehensive:
    """Comprehensive cold migration test covering multiple features.

    This test validates:
    - Static IP preservation
    - PVC name template functionality
    - PVC generateName support
    - Target node selector configuration
    - Target VM labels
    - Target VM affinity rules
    - Custom target namespace for VMs
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "BaseProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Create StorageMap resource.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details
            fixture_store (dict[str, Any]): Resource tracking dictionary
            source_provider (BaseProvider): Source provider connection
            destination_provider (BaseProvider): Destination provider connection
            ocp_admin_client (DynamicClient): OpenShift admin client
            target_namespace (str): Target namespace for migration resources
            source_provider_inventory (ForkliftInventory): Source provider inventory

        Raises:
            AssertionError: If StorageMap creation fails
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "BaseProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details
            fixture_store (dict[str, Any]): Resource tracking dictionary
            source_provider (BaseProvider): Source provider connection
            destination_provider (BaseProvider): Destination provider connection
            ocp_admin_client (DynamicClient): OpenShift admin client
            target_namespace (str): Target namespace for migration resources
            source_provider_inventory (ForkliftInventory): Source provider inventory
            multus_network_name (dict[str, str]): Multus network name for network mapping

        Raises:
            AssertionError: If NetworkMap creation fails
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        labeled_worker_node: dict[str, Any],
        target_vm_labels: dict[str, Any],
    ) -> None:
        """Create MTV Plan with comprehensive feature configuration.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details
            fixture_store (dict[str, Any]): Resource tracking dictionary
            source_provider (BaseProvider): Source provider connection
            destination_provider (OCPProvider): Destination provider connection
            ocp_admin_client (DynamicClient): OpenShift admin client
            target_namespace (str): Target namespace for migration resources
            source_provider_inventory (ForkliftInventory): Source provider inventory
            labeled_worker_node (dict[str, Any]): Worker node with label configuration
            target_vm_labels (dict[str, Any]): Target VM labels configuration

        Raises:
            AssertionError: If Plan creation fails
        """
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_power_state=prepared_plan["target_power_state"],
            warm_migration=prepared_plan["warm_migration"],
            preserve_static_ips=prepared_plan["preserve_static_ips"],
            pvc_name_template=prepared_plan["pvc_name_template"],
            pvc_name_template_use_generate_name=prepared_plan["pvc_name_template_use_generate_name"],
            target_node_selector={labeled_worker_node["label_key"]: labeled_worker_node["label_value"]},
            target_labels=target_vm_labels["vm_labels"],
            target_affinity=prepared_plan["target_affinity"],
            vm_target_namespace=prepared_plan["vm_target_namespace"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute migration with comprehensive features.

        Args:
            fixture_store (dict[str, Any]): Resource tracking dictionary
            ocp_admin_client (DynamicClient): OpenShift admin client
            target_namespace (str): Target namespace for migration resources

        Raises:
            MigrationTimeoutError: If migration fails to complete within timeout
        """
        execute_migration(
            fixture_store=fixture_store,
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        labeled_worker_node: dict[str, Any],
        target_vm_labels: dict[str, Any],
        vm_ssh_connections: "SSHConnectionManager | None",
    ) -> None:
        """Validate migrated VMs with comprehensive feature configuration.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details
            source_provider (BaseProvider): Source provider connection
            destination_provider (OCPProvider): Destination provider connection
            source_provider_data (dict[str, Any]): Source provider configuration
            source_vms_namespace (str): Source VMs namespace
            source_provider_inventory (ForkliftInventory): Source provider inventory
            labeled_worker_node (dict[str, Any]): Worker node with label configuration
            target_vm_labels (dict[str, Any]): Target VM labels configuration
            vm_ssh_connections: SSH connections fixture manager for connectivity testing

        Raises:
            AssertionError: If any VM validation checks fail
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_data=source_provider_data,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            labeled_worker_node=labeled_worker_node,
            target_vm_labels=target_vm_labels,
            vm_ssh_connections=vm_ssh_connections,
        )
