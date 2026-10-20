import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)
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
@pytest.mark.openstack
@pytest.mark.openshift
@pytest.mark.esxi
@pytest.mark.hyperv
@pytest.mark.tier0
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_sanity_cold_mtv_migration"],
        )
    ],
    indirect=True,
    ids=["rhel8"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestSanityColdMtvMigration:
    """Cold-migrate a Linux VM to OpenShift and verify it runs with equivalent resources.

    Purpose/Regression:
        Basic end-to-end cold migration sanity: a running Linux VM moved with
        a per-NIC network mapping must arrive powered as configured with
        equivalent CPU, memory, storage, and network. There is no induced
        failure; the scenario guards the core migration path against
        regressions in any provider type.

    Prerequisites:
        Register and connect a source provider (vSphere, RHV, OpenStack,
        OpenShift, ESXi, or Hyper-V) and an OpenShift destination with MTV
        installed. Prepare destination storage and network accessible to the
        migrated VM, including a secondary NetworkAttachmentDefinition when
        mapping per NIC. Use a disposable RHEL VM with a disk, at least one
        additional NIC (e.g. one that is not start-connected), and guest-agent
        access. Have permission to create maps, a Plan, and a target
        namespace.

    Test plan:
        1. Prepare the disposable VM and wait until it appears in Forklift
           inventory.
        2. Create a StorageMap for the VM's disks and a NetworkMap that maps
           each VM NIC to its own destination network. Confirm both are
           created.
        3. Create a cold Plan for the VM using both maps. Check the Plan's
           Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Validate the migrated VM: power state, guest-agent presence,
           connectivity when powered on, and CPU, memory, network, and
           storage equivalence with the source. After the checks, delete the
           migrated VM; the maps and Plan are removed with the run.

    Expected result:
        The migration succeeds and the migrated VM matches the source in
        CPU, memory, disks, and networks, with the configured power state and
        working connectivity. On failure, inspect the migration CR status for
        the VM's failed step, the Plan conditions, and the migrated VM's spec
        and events in the target namespace.
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
        """Create StorageMap resource for migration."""
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
        """Create NetworkMap resource for migration."""
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
            per_nic_network_map=prepared_plan.get("per_nic_network_map", False),
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
        """Create MTV Plan CR resource."""
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
            warm_migration=prepared_plan.get("warm_migration", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
    ):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs."""
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


@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.openstack
@pytest.mark.openshift
@pytest.mark.hyperv
@pytest.mark.remote
@pytest.mark.incremental
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote OCP cluster provided")
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_cold_remote_ocp"],
        )
    ],
    indirect=True,
    ids=["MTV-79"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestColdRemoteOcp:
    """Cold-migrate Linux and Windows VMs to a separate remote OpenShift cluster (MTV-79).

    Purpose/Regression:
        Migration where the destination is a different, remote OpenShift
        cluster than the one running MTV. Guards the cross-cluster path:
        provider, maps, and Plan are managed from the MTV cluster while the
        VMs land on the remote cluster. There is no induced failure; both a
        Linux and a Windows VM must migrate and validate.

    Prerequisites:
        Two OpenShift clusters: one running MTV (with the source provider and
        migration resources) and a remote destination cluster with accessible
        storage and network, registered with MTV and mutually reachable. Use
        disposable Linux and Windows VMs on the source with disks and
        networks supported by both providers. Have permission to create
        maps and a Plan on the MTV cluster and the target namespace on the
        remote cluster.

    Test plan:
        1. Prepare the disposable Linux and Windows VMs and wait until both
           appear in Forklift inventory.
        2. Create a StorageMap for the VMs' disks and a NetworkMap for their
           networks, targeting the remote OpenShift destination. Confirm both
           are created.
        3. Create a cold Plan for both VMs using the maps. Check the Plan's
           Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Validate the migrated VMs on the remote cluster: power state and
           CPU, memory, network, and storage equivalence with the sources.
           After the checks, delete the migrated VMs; the maps and Plan are
           removed with the run.

    Expected result:
        Both VMs migrate successfully to the remote cluster and match their
        sources in CPU, memory, disks, and networks. On failure, inspect the
        migration CR status for each VM's failed step, the remote provider
        resource's conditions on the MTV cluster, and the migrated VMs' specs
        and events in the remote target namespace.
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
        """Create StorageMap resource for migration."""
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
        """Create NetworkMap resource for migration."""
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
            per_nic_network_map=prepared_plan.get("per_nic_network_map", False),
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
        """Create MTV Plan CR resource."""
        populate_vm_ids(prepared_plan, source_provider_inventory)

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
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
    ):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_ocp_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs."""
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
        )
