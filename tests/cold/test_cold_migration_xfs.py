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
from utilities.post_migration import check_vms, check_vm_command_output
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_cold_migration_xfs"],
            id="xfs-cold",
        )
    ],
    indirect=True,
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestColdMigrationXfs:
    """Cold migrate VM with XFS v4 filesystem from vSphere.

    Test for following feature:
    - XFS v4 filesystem
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
        """Create StorageMap resource.

        Args:
            prepared_plan: Plan configuration with VM details
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for migration

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
        """Create NetworkMap resource with optional custom pod network.

        Args:
            prepared_plan: Plan configuration with VM details
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for migration
            multus_network_name: Dict with NAD base name and namespace

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
    ) -> None:
        """Create MTV Plan CR with XFS compatibility enabled.

        Args:
            prepared_plan: Plan configuration with VM details
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for migration
            source_provider_inventory: Source provider inventory

        Raises:
            AssertionError: If Plan creation fails
        """
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            target_power_state=prepared_plan["target_power_state"],
            warm_migration=prepared_plan.get("warm_migration", False),
            xfs_compatibility=prepared_plan.get("xfs_compatibility", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute cold migration.

        Args:
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            target_namespace: Target namespace for migration
        """
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_verify_xfs_version(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        vm_ssh_connections: "SSHConnectionManager",
    ) -> None:
        """Verify XFS filesystem on migrated VMs.

        Args:
            prepared_plan: Plan configuration with VM details
            source_provider: Source provider instance
            source_provider_data: Provider configuration from .providers.json
            source_vms_namespace: Source VMs namespace
            vm_ssh_connections: SSH connection manager for VMs
        """
        xfs_check = prepared_plan["xfs_check"]
        for vm in prepared_plan["virtual_machines"]:
            source_vm_info = source_provider.vm_dict(
                name=vm["name"],
                namespace=source_vms_namespace,
                source=True,
            )
            check_vm_command_output(
                vm=vm,
                vm_ssh_connections=vm_ssh_connections,
                source_vm_info=source_vm_info,
                source_provider_data=source_provider_data,
                command=[xfs_check["command"], xfs_check["mount_point"]],
                expected_output=xfs_check["expected_output"],
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
    ) -> None:
        """Validate migrated VMs.

        Args:
            prepared_plan: Plan configuration with VM details
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_data: Provider configuration from .providers.json
            source_vms_namespace: Source VMs namespace
            source_provider_inventory: Source provider inventory
            vm_ssh_connections: SSH connection manager for VMs
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
        )
