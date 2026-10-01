import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from exceptions.exceptions import MigrationPlanExecError
from utilities.migration_utils import get_cutover_value
from utilities.mtv_migration import (
    create_plan_resource,
    get_network_migration_map,
    get_storage_migration_map,
    wait_for_migration_complate,
)
from utilities.post_migration import check_vms
from utilities.resume_conversion import (
    execute_resume_migration,
    start_migration_and_kill_conversion,
    verify_pvcs_bound,
    verify_resume_skipped_disk_copy,
)
from utilities.utils import populate_vm_ids


@pytest.mark.vsphere
@pytest.mark.warm
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_resume_conversion_warm"])],
    indirect=True,
    ids=["MTV-6203-resume-conversion"],
)
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestResumeConversionWarmMigration:
    """Verify resumeConversion recovers a warm migration after conversion failure.

    Simulates conversion failure by killing the virt-v2v pod after disk copy,
    then creates a resume Migration CR that skips disk copy and re-runs conversion
    using the preserved PVCs.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
    pre_failure_pvc_uids: dict[str, str]

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
            target_namespace (str): Target namespace for migration.
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
            target_namespace (str): Target namespace for migration.
            multus_network_name (str): Name of the multus network.
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
        """Create warm migration Plan CR.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            target_namespace (str): Target namespace for migration.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
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
    ):
        """Start warm migration, record PVC UIDs, then kill conversion pod.

        Waits for the DiskTransfer pipeline step to complete, snapshots PVC UIDs
        (baseline for preservation checks), then waits for the virt-v2v pod
        (``forklift.app=virt-v2v``) and kills its processes. Verifies the migration
        fails specifically at the ImageConversion step.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration.
        """
        self.__class__.pre_failure_pvc_uids = start_migration_and_kill_conversion(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
        )
        with pytest.raises(MigrationPlanExecError, match="ImageConversion"):
            wait_for_migration_complate(plan=self.plan_resource)

    def test_verify_pvcs_preserved(
        self,
        ocp_admin_client,
        target_namespace,
    ):
        """Verify PVCs survived the conversion failure with unchanged UIDs.

        Compares current PVC UIDs against the baseline recorded before the
        conversion pod was killed.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration.
        """
        post_failure_pvc_uids = verify_pvcs_bound(
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
        )
        assert post_failure_pvc_uids == self.pre_failure_pvc_uids, (
            f"PVC UIDs changed after failure — PVCs were not preserved. "
            f"Before: {self.pre_failure_pvc_uids}, After: {post_failure_pvc_uids}"
        )

    def test_resume_migration(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
    ):
        """Resume migration after conversion failure.

        Creates a new Migration CR with ``resumeConversion: true``. Forklift
        handles cleanup of the failed conversion pod automatically. The resumed
        migration skips disk copy and re-runs only the conversion phase.
        Verifies PVC UIDs are unchanged (reuse, not recreate) and that the
        resumed pipeline did not re-execute DiskTransfer.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration.
        """
        execute_resume_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )
        resumed_pvc_uids = verify_pvcs_bound(
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
        )
        assert resumed_pvc_uids == self.pre_failure_pvc_uids, (
            f"PVC UIDs changed after resume — PVCs were recreated instead of reused. "
            f"Before: {self.pre_failure_pvc_uids}, Resumed: {resumed_pvc_uids}"
        )
        verify_resume_skipped_disk_copy(plan=self.plan_resource)

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
        """Validate migrated VMs after resume.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (BaseProvider): Destination provider instance.
            source_provider_data (dict[str, Any]): Source provider configuration data.
            target_namespace (str): Target namespace for migration.
            source_vms_namespace (str): Namespace of source VMs.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (dict[str, Any]): SSH connections to migrated VMs.
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


@pytest.mark.vsphere
@pytest.mark.warm
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_resume_conversion_warm"])],
    indirect=True,
    ids=["MTV-6205-ineligible-resume"],
)
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestResumeConversionIneligible:
    """Verify resumeConversion fails when no prior conversion failure exists (MTV-6205)."""

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
        """Create warm migration Plan CR."""
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

    def test_resume_without_prior_failure(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
    ):
        """Verify resumeConversion fails when no migration has run.

        Creates a resume Migration CR for a plan that has never been migrated.
        The resume should fail because there is no prior conversion failure and
        no preserved PVCs to reuse.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration.
        """
        with pytest.raises(MigrationPlanExecError):
            execute_resume_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.plan_resource,
                target_namespace=target_namespace,
            )
