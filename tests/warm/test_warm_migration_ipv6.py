from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.deep_inspection import DI_RESULTS_KEY, create_di_capture_callback
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
@pytest.mark.warm
@pytest.mark.ipv6
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_warm_migration_ipv6"],
            id="ipv6-warm",
        )
    ],
    indirect=True,
)
@pytest.mark.usefixtures("ipv6_esxi_host_required", "cleanup_migrated_vms", "precopy_interval_forkliftcontroller")
class TestWarmMigrationIpv6:
    """Warm-migrate VM from a vSphere ESXi host and validate the migrated VM.

    Purpose/Regression:
        Test for migrating Windows VM with IPv6 settings from ESXi IPv6 host to OCP cluster.
        A regression in IPv6 handling surfaces as the source provider failing to connect,
        or the migration never reaching Succeeded.

    Prerequisites:
        A vSphere ESXi host whose management endpoint is reachable over IPv6 (a direct ESXi
        connection, not vCenter), registered as the source provider, and an OpenShift destination
        with OpenShift Virtualization. A powered-on Windows guest with IPv6 setting.

    Test plan:
        1. Register the ESXi source provider over its IPv6 management endpoint and the OpenShift
           destination; confirm the Windows VM is powered on and visible in the source inventory.
        2. Create the StorageMap and NetworkMap mapping the source datastore and network to the
           destination storage class and target network; create the warm-migration Plan and
           confirm it reaches the Ready condition.
        3. Start the warm migration, let precopies run, trigger cutover, and observe the migration
           status progress to Succeeded — proving Forklift reached the ESXi host over IPv6.
        4. Validate the migrated VM: power state matches the requested target power state,
           the guest agent responds, and SSH connectivity to the VM succeeds.
        5. Delete the created StorageMap, NetworkMap, Plan, and migrated VM.

    Expected result:
        The Plan reaches Ready and the migration reaches Succeeded, confirming IPv6 source
        connectivity. The migrated VM runs in the target namespace with a responsive guest agent,
        working SSH. On failure, inspect the Plan and Migration status conditions and events
        for the IPv6 source connection, and the leftoverStorageMap, NetworkMap, Plan,
        and VirtualMachine resources.
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
    ) -> None:
        """Create MTV Plan CR with warm migration and static IP preservation enabled.

        Args:
            prepared_plan: Plan configuration with VM details
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for migration
            source_provider_inventory: Source provider inventory

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
            target_namespace=target_namespace,
            target_power_state=prepared_plan["target_power_state"],
            warm_migration=prepared_plan.get("warm_migration", False),
            preserve_static_ips=prepared_plan.get("preserve_static_ips", False),
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
        di_callback = create_di_capture_callback(
            plan=self.plan_resource,
            fixture_store=fixture_store,
        )
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
            on_status_poll=di_callback,
        )

    def test_check_vms(
        self,
        fixture_store: dict[str, Any],
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager",
    ) -> None:
        """Validate migrated VMs and static IPv4 preservation.

        Args:
            fixture_store: Fixture store for DI result retrieval
            prepared_plan: Plan configuration with VM details
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_data: Provider configuration from .providers.json
            source_vms_namespace: Source VMs namespace
            source_provider_inventory: Source provider inventory
            vm_ssh_connections: SSH connection manager for VMs

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
            plan_resource=self.plan_resource,
            di_results=fixture_store.get(DI_RESULTS_KEY),
        )
