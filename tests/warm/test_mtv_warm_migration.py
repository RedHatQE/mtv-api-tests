import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.provider import Provider
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import py_config

from utilities.deep_inspection import DI_RESULTS_KEY, create_di_capture_callback
from utilities.migration_utils import get_cutover_value
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms
from utilities.utils import get_value_from_py_config, populate_vm_ids


@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.upgrade
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_sanity_warm_mtv_migration"],
        )
    ],
    indirect=True,
    ids=["rhel8"],
)
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestSanityWarmMtvMigration:
    """Warm-migrate a running Linux VM with static IP preservation and verify the result including inspection data.

    Purpose/Regression:
        Basic end-to-end warm migration sanity: a powered-on VM is copied
        incrementally and finalized at cutover, with its static IP
        configuration preserved on the destination (verified for vSphere
        and Hyper-V sources). On a vSphere source the
        scenario also verifies the warm-migration Deep Inspection results
        captured during pre-copy. There is no induced failure.

    Prerequisites:
        Register and connect a source provider (vSphere or RHV) and an
        OpenShift destination with MTV installed, with accessible destination
        storage and network. Use a disposable, powered-on RHEL VM with
        guest-agent access, at least one NIC configured with a static IP
        (address, subnet, gateway), and disks whose changes can be captured
        incrementally (e.g. changed-block tracking on vSphere). Have
        permission to create maps and a Plan in a target namespace and to
        adjust the controller's pre-copy interval for a timely scenario.
        When migrating from vSphere, warm-migration Deep Inspection must be
        available.

    Test plan:
        1. Prepare the disposable powered-on VM with its static IP
           configuration and wait until it appears in Forklift inventory.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network. Confirm both are created.
        3. Create a warm Plan for the VM with static IP preservation
           enabled. Check the Plan's Ready condition is True.
        4. Run the warm migration with a cutover time: incremental copies
           run while the VM stays on, the migration finalizes at cutover.
           Check it reaches Succeeded. On a vSphere source, the inspection
           data captured during the migration is recorded.
        5. Validate the migrated VM: power state, guest-agent presence,
           connectivity, and CPU, memory, network, and storage equivalence
           with the source. When migrating from vSphere or Hyper-V, also
           verify static IP/subnet/gateway preservation; on vSphere, also
           NIC name preservation. On a vSphere source, also verify the
           captured warm-migration Deep Inspection results are present and
           complete. After the checks, delete the migrated VM; the maps and
           Plan are removed with the run.

    Expected result:
        The warm migration completes through cutover, the migrated VM keeps
        its static network configuration and connectivity, and (on vSphere)
        the inspection results captured during pre-copy are complete. On
        failure, inspect the migration CR status for the failed stage
        (pre-copy or cutover), the guest network configuration inside the
        migrated VM, and the Plan-created DeepInspection Conversions' results.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
    ):
        """Create StorageMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (Namespace): Target namespace for migration.

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails.
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
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (Namespace): Target namespace for migration.
            multus_network_name (str): Name of the multus network.

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails.
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
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            target_namespace (Namespace): Target namespace for migration.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails.
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
            warm_migration=prepared_plan.get("warm_migration", False),
            preserve_static_ips=prepared_plan.get("preserve_static_ips", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
        source_provider,
    ):
        """Execute warm migration with cutover.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (Namespace): Target namespace for migration.
            source_provider (BaseProvider): Source provider, used to gate DI capture to vSphere.

        Returns:
            None
        """
        di_callback = (
            create_di_capture_callback(plan=self.plan_resource, fixture_store=fixture_store)
            if source_provider.type == Provider.ProviderType.VSPHERE
            else None
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
        fixture_store,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs.

        Args:
            fixture_store (dict[str, Any]): Fixture store for DI result retrieval.
            prepared_plan (dict[str, Any]): The prepared migration plan.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            source_provider_data (dict[str, Any]): Source provider configuration data.
            target_namespace (Namespace): Target namespace for migration.
            source_vms_namespace (str): Namespace of source VMs.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (dict[str, Any]): SSH connections to migrated VMs.

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


@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.warm
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_mtv_migration_warm_2disks2nics"],
        )
    ],
    indirect=True,
    ids=["MTV-200 rhel"],
)
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestMtvMigrationWarm2disks2nics:
    """Warm-migrate a running VM with two disks and two NICs and verify both survive cutover (MTV-200).

    Purpose/Regression:
        Warm migration must handle a VM with multiple disks and multiple
        network interfaces: both disks must be copied incrementally and
        finalized at cutover, and both NICs must appear on the migrated VM
        per the NetworkMap (MTV-200). There is no induced failure.

    Prerequisites:
        Register and connect a source provider (vSphere or RHV) and an
        OpenShift destination with MTV installed, with accessible destination
        storage and network (including a destination network for the second
        NIC). Use a disposable, powered-on RHEL VM with two disks, two NICs,
        and guest-agent access, with disks whose changes can be captured
        incrementally. Have permission to create maps and a Plan in a target
        namespace and to adjust the controller's pre-copy interval.

    Test plan:
        1. Prepare the disposable powered-on VM with its two disks and two
           NICs and wait until it appears in Forklift inventory.
        2. Create a StorageMap for both disks and a NetworkMap that maps
           both NICs to destination networks. Confirm both are created.
        3. Create a warm Plan for the VM. Check the Plan's Ready condition
           is True.
        4. Run the warm migration with a cutover time and check it reaches
           Succeeded.
        5. Validate the migrated VM: power state, guest-agent presence,
           connectivity, and CPU, memory, network (both NICs), and storage
           (both disks) equivalence with the source. After the checks,
           delete the migrated VM; the maps and Plan are removed with the
           run.

    Expected result:
        The warm migration completes through cutover with both disks and
        both NICs present and equivalent on the migrated VM. On failure,
        inspect the migration CR status for the failed stage and the
        migrated VM's disk attachments and network interfaces.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
    ):
        """Create StorageMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (Namespace): Target namespace for migration.

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails.
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
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (Namespace): Target namespace for migration.
            multus_network_name (str): Name of the multus network.

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails.
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
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            target_namespace (Namespace): Target namespace for migration.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails.
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
            warm_migration=prepared_plan.get("warm_migration", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
        source_provider,
    ):
        """Execute warm migration with cutover.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (Namespace): Target namespace for migration.
            source_provider (BaseProvider): Source provider, used to gate DI capture to vSphere.

        Returns:
            None
        """
        di_callback = (
            create_di_capture_callback(plan=self.plan_resource, fixture_store=fixture_store)
            if source_provider.type == Provider.ProviderType.VSPHERE
            else None
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
        fixture_store,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs.

        Args:
            fixture_store (dict[str, Any]): Fixture store for DI result retrieval.
            prepared_plan (dict[str, Any]): The prepared migration plan.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            source_provider_data (dict[str, Any]): Source provider configuration data.
            target_namespace (Namespace): Target namespace for migration.
            source_vms_namespace (str): Namespace of source VMs.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (dict[str, Any]): SSH connections to migrated VMs.

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


@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.warm
@pytest.mark.remote
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_warm_remote_ocp"],
        )
    ],
    indirect=True,
    ids=["MTV-394"],
)
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote OCP cluster provided")
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestWarmRemoteOcp:
    """Warm-migrate a running VM to a separate remote OpenShift cluster and verify the result.

    Purpose/Regression:
        Warm migration where the destination is a different, remote
        OpenShift cluster than the one running MTV: the incremental copies
        and cutover must run against the remote destination and the migrated
        VM must validate there. Preflight Deep Inspection is disabled for
        this scenario. There is no induced failure.

    Prerequisites:
        Two OpenShift clusters: one running MTV (with the source provider
        and migration resources) and a remote destination cluster with
        accessible storage and network, registered with MTV and mutually
        reachable. Register and connect a source provider (e.g. vSphere).
        Use a disposable, powered-on Linux VM with guest-agent access and
        disks whose changes can be captured incrementally. Have permission
        to create maps and a Plan on the MTV cluster and the target
        namespace on the remote cluster.

    Test plan:
        1. Prepare the disposable powered-on VM and wait until it appears
           in Forklift inventory.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network, targeting the remote OpenShift destination. Confirm both
           are created.
        3. Create a warm Plan for the VM with preflight inspection
           disabled. Check the Plan's Ready condition is True.
        4. Run the warm migration with a cutover time and check it reaches
           Succeeded.
        5. Validate the migrated VM on the remote cluster: power state,
           guest-agent presence, connectivity, and CPU, memory, network,
           and storage equivalence with the source. After the checks, delete
           the migrated VM; the maps and Plan are removed with the run.

    Expected result:
        The warm migration to the remote cluster completes through cutover
        and the migrated VM validates. On failure, inspect the migration CR
        status for the failed stage (pre-copy or cutover), the remote
        provider resource's conditions on the MTV cluster, and the migrated
        VM's spec and events in the remote target namespace.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_ocp_provider,
        source_provider_inventory,
        target_namespace,
    ):
        """Create StorageMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_ocp_provider (BaseProvider): Destination OCP provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (Namespace): Target namespace for migration.

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails.
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_ocp_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_ocp_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_ocp_provider (BaseProvider): Destination OCP provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (Namespace): Target namespace for migration.
            multus_network_name (str): Name of the multus network.

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails.
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_ocp_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_ocp_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_ocp_provider (BaseProvider): Destination OCP provider instance.
            target_namespace (Namespace): Target namespace for migration.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails.
        """
        populate_vm_ids(plan=prepared_plan, inventory=source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_ocp_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            run_preflight_inspection=prepared_plan.get("run_preflight_inspection"),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
        source_provider,
    ):
        """Execute warm migration with cutover.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (Namespace): Target namespace for migration.
            source_provider (BaseProvider): Source provider, used to gate DI capture to vSphere.

        Returns:
            None
        """
        di_callback = (
            create_di_capture_callback(plan=self.plan_resource, fixture_store=fixture_store)
            if source_provider.type == Provider.ProviderType.VSPHERE
            else None
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
        fixture_store,
        prepared_plan,
        source_provider,
        destination_ocp_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs.

        Args:
            fixture_store (dict[str, Any]): Fixture store for DI result retrieval.
            prepared_plan (dict[str, Any]): The prepared migration plan.
            source_provider (BaseProvider): Source provider instance.
            destination_ocp_provider (BaseProvider): Destination OCP provider instance.
            source_provider_data (dict[str, Any]): Source provider configuration data.
            target_namespace (Namespace): Target namespace for migration.
            source_vms_namespace (str): Namespace of source VMs.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (dict[str, Any]): SSH connections to migrated VMs.

        Returns:
            None
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_ocp_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
            plan_resource=self.plan_resource,
            di_results=fixture_store.get(DI_RESULTS_KEY),
        )
