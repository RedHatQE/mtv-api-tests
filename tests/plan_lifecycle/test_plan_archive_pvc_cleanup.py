"""MTV-5663: Verify PVC cleanup after archiving and deleting a failed migration plan.

Regression test for MTV-5564: archiving and deleting a failed plan left
orphan PVCs (both regular and prime PVCs) in the target namespace.

This test induces failure via a post-hook (not mid-transfer like the original
bug) to create PVCs and then fail the migration. Both paths exercise the same
Forklift plan archive+delete cleanup mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.datavolume import DataVolume
from ocp_resources.migration import Migration
from ocp_resources.network_map import NetworkMap
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine import VirtualMachine
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import MigrationPlanExecError
from utilities.hooks import validate_hook_failure_and_check_vms
from utilities.migration_utils import archive_plan
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_migration_for_plan,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.naming import resolve_destination_vm_name
from utilities.resources import unregister_teardown_resource
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider


_ORPHAN_RESOURCE_WAIT_TIMEOUT = 120  # Seconds to wait for async DV/PVC garbage collection
_ORPHAN_RESOURCE_POLL_INTERVAL = 5  # Seconds between polls


def _get_orphan_resource_names(ocp_admin_client: DynamicClient, target_namespace: str) -> list[str]:
    """List remaining DV and PVC names in the target namespace.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace to check.

    Returns:
        list[str]: Names of remaining DVs and PVCs, empty if none.
    """
    remaining_pvcs = list(PersistentVolumeClaim.get(client=ocp_admin_client, namespace=target_namespace))
    remaining_dvs = list(DataVolume.get(client=ocp_admin_client, namespace=target_namespace))
    return [pvc.name for pvc in remaining_pvcs] + [dv.name for dv in remaining_dvs]


@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.openstack
@pytest.mark.openshift
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_plan_archive_pvc_cleanup"])],
    indirect=True,
    ids=["MTV-5663-plan-archive-pvc-cleanup"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestPlanArchivePvcCleanup:
    """MTV-5663: Verify PVC cleanup after archiving and deleting a failed migration plan.

    Regression test for MTV-5564: archiving and deleting a failed plan left
    orphan PVCs (both regular and prime PVCs) in the target namespace.

    Test steps:
        1. Create StorageMap resource.
        2. Create NetworkMap resource.
        3. Create Plan with a post-hook configured to fail.
        4. Execute migration — migration runs far enough to create PVCs,
           then fails due to post-hook failure (MigrationPlanExecError).
        5. Archive the failed plan, then delete it.
        6. Delete retained destination VMs, then verify all DVs and PVCs
           in the target namespace are cleaned up — no orphan resources
           remain (including prime-* PVCs).
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace for migration.

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
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace for migration.
            multus_network_name (dict[str, str]): Name of the multus network.

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
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR with a post-hook configured to fail.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            target_namespace (str): Target namespace for migration.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Raises:
            AssertionError: If Plan creation fails.
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
            warm_migration=prepared_plan.get("warm_migration", False),
            target_power_state=prepared_plan["target_power_state"],
            after_hook_name=prepared_plan["_post_hook_name"],
            after_hook_namespace=prepared_plan["_post_hook_namespace"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute migration — expected to fail due to post-hook failure.

        The migration runs far enough to create PVCs for the VM disks, then
        the post-hook triggers a failure. This leaves PVCs in the target
        namespace that should be cleaned up when the plan is archived and deleted.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration.

        Raises:
            AssertionError: If migration does not fail at PostHook as expected.
        """
        with pytest.raises(MigrationPlanExecError):
            execute_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.plan_resource,
                target_namespace=target_namespace,
            )

        validate_hook_failure_and_check_vms(self.plan_resource, prepared_plan)

        # Verify migration created resources before we archive+delete.
        # Filter by session_uuid to scope to resources from this test run,
        # avoiding false positives if vm_target_namespace is shared.
        vm_namespace = prepared_plan.get("_vm_target_namespace", target_namespace)
        session_uuid = fixture_store["session_uuid"]
        migration_pvcs = [
            pvc
            for pvc in PersistentVolumeClaim.get(client=ocp_admin_client, namespace=vm_namespace)
            if session_uuid in pvc.name
        ]
        migration_dvs = [
            dv for dv in DataVolume.get(client=ocp_admin_client, namespace=vm_namespace) if session_uuid in dv.name
        ]
        assert migration_pvcs or migration_dvs, (
            f"No PVCs or DataVolumes with session '{session_uuid}' found in "
            f"namespace '{vm_namespace}' after post-hook failure — "
            "the archive+delete cleanup assertion would be vacuous"
        )

    def test_archive_and_delete_plan(self, fixture_store: dict[str, Any]) -> None:
        """Archive and delete the failed migration plan.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.

        Raises:
            AssertionError: If plan is not archived or deletion fails.
        """
        plan = self.plan_resource
        migration_name = get_migration_for_plan(plan).name

        archive_plan(plan=plan)
        conditions = plan.instance.status.conditions or []
        assert any(
            condition["type"] == plan.Condition.ARCHIVED and condition["status"] == plan.Condition.Status.TRUE
            for condition in conditions
        ), f"Plan '{plan.name}' did not reach Archived condition"

        assert plan.clean_up(wait=True), f"Failed to delete plan '{plan.name}' after archiving"

        # Plan was deleted intentionally; unregister so session_teardown does not
        # call archive_plan() on a missing Plan and abort the rest of cleanup.
        unregister_teardown_resource(fixture_store=fixture_store, kind=Plan.kind, name=plan.name)
        unregister_teardown_resource(fixture_store=fixture_store, kind=Migration.kind, name=migration_name)

    def test_verify_pvc_cleanup(
        self,
        prepared_plan: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Verify all PVCs are cleaned up after plan archive and deletion.

        Destination VMs may still exist if post-hook failure retained them.
        Any remaining VMs are deleted so the orphan DV/PVC check below is not
        masked by VM-owned resources.
        Polls for up to 120s because DV/PVC garbage collection is async.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration.

        Raises:
            AssertionError: If orphan resources remain after 120s timeout.
        """
        vm_namespace = prepared_plan.get("_vm_target_namespace", target_namespace)
        for vm in prepared_plan["virtual_machines"]:
            vm_name = resolve_destination_vm_name(vm)
            vm_obj = VirtualMachine(client=ocp_admin_client, name=vm_name, namespace=vm_namespace)
            if vm_obj.exists:
                vm_obj.clean_up(wait=True)

        try:
            for sample in TimeoutSampler(
                wait_timeout=_ORPHAN_RESOURCE_WAIT_TIMEOUT,
                sleep=_ORPHAN_RESOURCE_POLL_INTERVAL,
                func=_get_orphan_resource_names,
                ocp_admin_client=ocp_admin_client,
                target_namespace=vm_namespace,
            ):
                if not sample:
                    return
        except TimeoutExpiredError as err:
            orphan_names = _get_orphan_resource_names(ocp_admin_client=ocp_admin_client, target_namespace=vm_namespace)
            if orphan_names:
                raise AssertionError(
                    f"Orphan resources remain in namespace '{vm_namespace}' after plan archive+delete: {orphan_names}"
                ) from err
