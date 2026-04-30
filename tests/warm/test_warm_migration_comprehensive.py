from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.migration_utils import get_cutover_value
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


@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_warm_migration_comprehensive"],
        )
    ],
    indirect=True,
    ids=["comprehensive-warm"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms", "precopy_interval_forkliftcontroller")
class TestWarmMigrationComprehensive:
    """Comprehensive warm migration test covering multiple features.

    Tests the following MTV 2.10.0+ features:
    - Static IP preservation
    - Custom VM target namespace
    - Custom PVC naming template
    - PVC naming with generateName
    - Target VM labels
    - Target VM affinity rules
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource for migration.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for migration

        Returns:
            None

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
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for migration.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for migration
            multus_network_name: Name of the multus network

        Returns:
            None

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
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        target_vm_labels: dict[str, Any],
    ) -> None:
        """Create MTV Plan CR resource with comprehensive features.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for migration
            source_provider_inventory: Source provider inventory
            target_vm_labels: Target VM labels configuration

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails
        """
        populate_vm_ids(plan=prepared_plan, inventory=source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_power_state=prepared_plan["target_power_state"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan["warm_migration"],
            preserve_static_ips=prepared_plan["preserve_static_ips"],
            vm_target_namespace=prepared_plan["vm_target_namespace"],
            pvc_name_template=prepared_plan["pvc_name_template"],
            pvc_name_template_use_generate_name=prepared_plan["pvc_name_template_use_generate_name"],
            target_labels=target_vm_labels["vm_labels"],
            target_affinity=prepared_plan["target_affinity"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute warm migration with cutover.

        Args:
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            target_namespace: Target namespace for migration

        Returns:
            None
        """
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager",
        target_vm_labels: dict[str, Any],
    ) -> None:
        """Validate migrated VMs with comprehensive features.

        Args:
            prepared_plan: The prepared migration plan
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_data: Source provider configuration data
            source_vms_namespace: Namespace of source VMs
            source_provider_inventory: Source provider inventory
            vm_ssh_connections: SSH connections to migrated VMs
            target_vm_labels: Target VM labels configuration

        Returns:
            None
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
            target_vm_labels=target_vm_labels,
        )
