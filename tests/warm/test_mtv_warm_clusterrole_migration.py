import pytest
from exceptions.exceptions import MigrationPlanExecError
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
from utilities.utils import get_value_from_py_config, populate_vm_ids


@pytest.mark.vsphere
@pytest.mark.remote
@pytest.mark.tier1
@pytest.mark.warm
@pytest.mark.incremental
@pytest.mark.skipif(
    not get_value_from_py_config("remote_ocp_cluster"),
    reason="No remote OCP cluster provided",
)
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_mtv_clusterrole_warm_migration"])],
    indirect=True,
    ids=["MTV-3129-clusterrole-warm"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestClusterroleWarmMtvMigration:
    """Warm migration to a remote cluster via a token provider bound only to forklift-migrator-role must fail (MTV-3129).

    Purpose/Regression:
        A destination OpenShift provider registered with a token whose
        ServiceAccount is bound only to the forklift-migrator-role ClusterRole
        lacks the SecurityContextConstraints permissions warm migration
        needs, so the migration must fail rather than half-complete
        (MTV-3129). The chosen failure path is the missing SCC binding; the
        migration is expected to fail and no destination VM is validated.

    Prerequisites:
        A remote OpenShift cluster with MTV installed and accessible storage
        and network, registered from the main MTV cluster as a token-based
        destination provider: create a ServiceAccount in the MTV operator
        namespace, bind it only to the existing forklift-migrator-role
        ClusterRole, and use its token for the Provider CR. Register and
        connect a vSphere source provider. Use a disposable, powered-on
        Linux VM with guest-agent access and disks whose changes can be
        captured incrementally. Have permission to create maps and a Plan.
        The destination must NOT have the forklift-controller SCC bound to
        the migration namespace's default ServiceAccount.

    Test plan:
        1. Prepare the disposable powered-on VM, wait until it appears in
           Forklift inventory, and register the remote destination provider
           with the migrator-role-only token.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network against the remote destination. Confirm both are created.
        3. Create a warm Plan for the VM. Check the Plan's Ready condition
           is True.
        4. Run the warm migration with a cutover and check that it fails
           (the migration pods cannot run with the required security
           context on the remote cluster). The failed migration resources,
           maps, and Plan are removed with the run.

    Expected result:
        The warm migration fails rather than completing, because the
        token-bound provider lacks SCC permissions. On failure of the
        expectation (the migration succeeds), inspect the migration CR
        status for the failed step and the forklift controller logs on the
        remote cluster for security-context admission errors.
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
        clusterrole_destination_ocp_provider,
        source_provider_inventory,
        target_namespace,
    ):
        """Create StorageMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=clusterrole_destination_ocp_provider,
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
        clusterrole_destination_ocp_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=clusterrole_destination_ocp_provider,
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
        clusterrole_destination_ocp_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=clusterrole_destination_ocp_provider,
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
    ):
        """Execute warm migration, expecting failure without SCC binding."""
        with pytest.raises(MigrationPlanExecError):
            execute_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.plan_resource,
                target_namespace=target_namespace,
                cut_over=get_cutover_value(),
            )


@pytest.mark.vsphere
@pytest.mark.remote
@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.incremental
@pytest.mark.skipif(
    not get_value_from_py_config("remote_ocp_cluster"),
    reason="No remote OCP cluster provided",
)
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_mtv_clusterrole_warm_migration_with_scc"])],
    indirect=True,
    ids=["MTV-3129-clusterrole-warm-with-scc"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestClusterroleWarmWithSccMigration:
    """Warm migration to a remote cluster via a token provider succeeds once the forklift-controller SCC is bound (MTV-3129).

    Purpose/Regression:
        The positive counterpart to the migrator-role-only failure: when the
        migration namespace's default ServiceAccount on the remote
        destination is additionally granted the forklift-controller
        SecurityContextConstraints (via a ClusterRole and RoleBinding), the
        same token-based provider setup must complete a warm migration
        successfully (MTV-3129). There is no induced failure.

    Prerequisites:
        A remote OpenShift cluster with MTV installed and accessible storage
        and network, registered from the main MTV cluster as a token-based
        destination provider: create a ServiceAccount in the MTV operator
        namespace, bind it to the forklift-migrator-role ClusterRole, and use
        its token for the Provider CR. Grant the forklift-controller SCC to
        the migration namespace's default ServiceAccount (e.g. with an SCC
        ClusterRole and RoleBinding, equivalent to
        ``oc adm policy add-scc-to-user forklift-controller-scc -z default``).
        Register and connect a vSphere source provider. Use a disposable,
        powered-on Linux VM with guest-agent access. Have permission to
        create maps, a Plan, and the SCC binding.

    Test plan:
        1. Prepare the disposable powered-on VM, wait until it appears in
           Forklift inventory, register the remote destination provider with
           the token, and create the SCC binding for the migration
           namespace.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network against the remote destination. Confirm both are created.
        3. Create a warm Plan for the VM. Check the Plan's Ready condition
           is True.
        4. Run the warm migration with a cutover and check it reaches
           Succeeded.
        5. Validate the migrated VM on the remote cluster: power state,
           guest-agent presence, connectivity, and CPU, memory, network,
           and storage equivalence with the source. After the checks, delete
           the migrated VM; the SCC binding, provider, maps, and Plan are
           removed with the run.

    Expected result:
        The warm migration completes successfully with the SCC binding in
        place, and the migrated VM validates. On failure, inspect the
        migration CR status for the failed step, the SCC RoleBinding in the
        migration namespace, and the forklift controller logs on the remote
        cluster for security-context admission errors.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        forklift_scc_binding,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        clusterrole_destination_ocp_provider,
        source_provider_inventory,
        target_namespace,
    ):
        """Create StorageMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=clusterrole_destination_ocp_provider,
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
        clusterrole_destination_ocp_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=clusterrole_destination_ocp_provider,
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
        clusterrole_destination_ocp_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=clusterrole_destination_ocp_provider,
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
    ):
        """Execute warm migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        clusterrole_destination_ocp_provider,
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
            destination_provider=clusterrole_destination_ocp_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
