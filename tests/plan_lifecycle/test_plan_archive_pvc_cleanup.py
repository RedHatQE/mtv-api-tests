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
_FAILED_MIGRATION_RESOURCE_WAIT_TIMEOUT = 120  # Seconds to wait for DVs/PVCs after Plan FAILED
_FAILED_MIGRATION_RESOURCE_POLL_INTERVAL = 5  # Seconds between polls


def _namespace_dv_and_pvc_names(client: DynamicClient, namespace: str) -> tuple[list[str], list[str]]:
    """List PVC and DataVolume names in a namespace.

    Args:
        client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace to list.

    Returns:
        tuple[list[str], list[str]]: PVC names, then DataVolume names.
    """
    pvc_names = [pvc.name for pvc in PersistentVolumeClaim.get(client=client, namespace=namespace)]
    dv_names = [dv.name for dv in DataVolume.get(client=client, namespace=namespace)]
    return pvc_names, dv_names


def _failed_migration_resources_ready(client: DynamicClient, namespace: str) -> bool:
    """Return whether a failed migration left a DV, a regular PVC, and a prime PVC.

    Args:
        client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace to inspect.

    Returns:
        bool: True when all three resource types are visible.
    """
    pvc_names, dv_names = _namespace_dv_and_pvc_names(client=client, namespace=namespace)
    return (
        bool(dv_names)
        and any(not name.startswith("prime-") for name in pvc_names)
        and any(name.startswith("prime-") for name in pvc_names)
    )


def _get_orphan_resource_names(client: DynamicClient, namespace: str) -> list[str]:
    """List remaining DV and PVC names in a namespace.

    The target namespace is unique per session (named after session_uuid),
    so all PVCs/DVs in it belong to this test run.

    Args:
        client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace to check.

    Returns:
        list[str]: Prefixed names (PVC/name, DV/name) of remaining resources, empty if none.
    """
    pvc_names, dv_names = _namespace_dv_and_pvc_names(client=client, namespace=namespace)
    return [f"PVC/{name}" for name in pvc_names] + [f"DV/{name}" for name in dv_names]


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
    """Verify failed-Plan archive and deletion clean up destination DVs and PVCs.

    Purpose/Regression:
        MTV-5663 covers the PVC leak reported in MTV-5564: archiving and
        deleting a failed Plan left regular and prime PVCs behind. The original
        failure occurred mid-transfer. This test fails at PostHook, after disk
        resources exist, then exercises the same Plan archive/delete cleanup.

    Prerequisites:
        Register and connect an MTV source provider and OpenShift destination.
        Prepare accessible destination storage and network for the source VM.
        Use a disposable, powered-on Linux source VM
        with a disk and network interface supported by both providers and
        guest-agent access for migration checks. For an OpenShift source,
        create the VM from an available OS DataSource such as ``rhel9``.
        Have permission to create a dedicated target namespace, maps, a
        Hook, and a Plan. Use a cold migration, target power state off,
        and a post-migration Hook that intentionally fails.

    Test plan:
        1. Prepare the disposable source VM and start it. For an OpenShift
           source, create and attach a dedicated source network. Wait until
           the VM appears in Forklift inventory. Create a dedicated target
           namespace and a post-migration Hook whose playbook fails.
        2. Create a StorageMap for the prepared VM's disks and a NetworkMap
           for its network. Create a cold Plan using those maps, the failing
           PostHook, and target VM power state off. Wait for its Ready
           condition to become True.
        3. Run the migration. Check that the migration fails and that the
           VM's failed step in the Plan is PostHook.
        4. Before archiving, check the effective VM target namespace for at
           least one DataVolume, one regular PVC, and one prime- prefixed PVC.
           Allow up to 120 seconds for these objects to become visible.
        5. Archive the failed Plan. Check its Archived condition is True, then
           delete the Plan and confirm both the Plan and its Migration are gone.
        6. Delete any retained destination VM in the target namespace and
           wait for its deletion to finish.
        7. Poll that namespace for up to 120 seconds for any remaining
           DataVolumes or PVCs, not just names matching the VM. After the
           check, remove the disposable source VM, maps, Hook, dedicated
           namespace, and any source network created in step 1.

    Expected result:
        1. The migration fails at PostHook after a DataVolume, a regular PVC,
           and a prime PVC have been observed in the effective target namespace.
        2. The failed Plan reaches Archived=True; deleting it also removes its
           Migration. If the Migration remains, report its name and namespace.
        3. After deletion of any retained destination VM, no DataVolumes or
           PVCs remain in the isolated effective target namespace within 120
           seconds. If a check fails, inspect the Plan conditions and the VM's
           failed step for PostHook, review the failing Hook's events or logs,
           then list remaining PVC and DataVolume names in the effective VM
           target namespace.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],  # Any: dynamic pytest plan config
        fixture_store: dict[str, Any],  # Any: pytest fixture_store has dynamic teardown structure
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
        assert self.__class__.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],  # Any: dynamic pytest plan config
        fixture_store: dict[str, Any],  # Any: pytest fixture_store has dynamic teardown structure
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
        assert self.__class__.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],  # Any: dynamic pytest plan config
        fixture_store: dict[str, Any],  # Any: pytest fixture_store has dynamic teardown structure
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

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails.
        """
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.__class__.storage_map,
            network_map=self.__class__.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            target_power_state=prepared_plan["target_power_state"],
            after_hook_name=prepared_plan["_post_hook_name"],
            after_hook_namespace=prepared_plan["_post_hook_namespace"],
        )
        assert self.__class__.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        prepared_plan: dict[str, Any],  # Any: dynamic pytest plan config
        fixture_store: dict[str, Any],  # Any: pytest fixture_store has dynamic teardown structure
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

        Returns:
            None

        Raises:
            AssertionError: If migration does not fail at PostHook as expected.
        """
        with pytest.raises(MigrationPlanExecError):
            execute_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.__class__.plan_resource,
                target_namespace=target_namespace,
            )

        validate_hook_failure_and_check_vms(self.__class__.plan_resource, prepared_plan)

        # Verify migration created resources before we archive+delete.
        # The target namespace is unique per session (named after session_uuid),
        # so all PVCs/DVs in it belong to this test run. Forklift creates PVCs
        # using source disk UUIDs (not session_uuid), so name filtering is wrong.
        # execute_migration() returns when Plan is FAILED and does not wait for
        # DV/PVC objects to be visible, so poll until they appear.
        vm_namespace = prepared_plan.get("_vm_target_namespace", target_namespace)
        try:
            for sample in TimeoutSampler(
                wait_timeout=_FAILED_MIGRATION_RESOURCE_WAIT_TIMEOUT,
                sleep=_FAILED_MIGRATION_RESOURCE_POLL_INTERVAL,
                func=_failed_migration_resources_ready,
                client=ocp_admin_client,
                namespace=vm_namespace,
            ):
                if sample:
                    break
        except TimeoutExpiredError as exc:
            pvc_names, dv_names = _namespace_dv_and_pvc_names(client=ocp_admin_client, namespace=vm_namespace)
            raise AssertionError(
                f"Failed-migration DVs/PVCs not visible in namespace '{vm_namespace}' "
                f"within {_FAILED_MIGRATION_RESOURCE_WAIT_TIMEOUT}s. PVCs={pvc_names} DVs={dv_names}"
            ) from exc

    def test_archive_and_delete_plan(
        self,
        fixture_store: dict[str, Any],  # Any: pytest fixture_store has dynamic teardown structure
    ) -> None:
        """Archive and delete the failed migration plan.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.

        Returns:
            None

        Raises:
            AssertionError: If plan is not archived or Plan/Migration deletion fails.
        """
        plan = self.__class__.plan_resource
        migration = get_migration_for_plan(plan)

        archive_plan(plan=plan)
        conditions = plan.instance.status.conditions or []
        assert any(
            condition["type"] == plan.Condition.ARCHIVED and condition["status"] == plan.Condition.Status.TRUE
            for condition in conditions
        ), f"Plan '{plan.name}' did not reach Archived condition"

        assert plan.clean_up(wait=True), f"Failed to delete plan '{plan.name}' after archiving"

        # Plan is gone, but keep the Migration tracked until cascade deletion completes.
        unregister_teardown_resource(fixture_store=fixture_store, resource=plan)
        assert migration.wait_deleted(timeout=120), (
            f"Migration '{migration.name}' in namespace '{migration.namespace}' was not deleted "
            f"within 120s after Plan '{plan.name}' deletion; retained for session cleanup"
        )
        unregister_teardown_resource(fixture_store=fixture_store, resource=migration)

    def test_verify_pvc_cleanup(
        self,
        prepared_plan: dict[str, Any],  # Any: dynamic pytest plan config
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

        Returns:
            None

        Raises:
            AssertionError: If orphan resources remain after 120s timeout.
        """
        vm_namespace = prepared_plan.get("_vm_target_namespace", target_namespace)
        for vm in prepared_plan["virtual_machines"]:
            vm_name = resolve_destination_vm_name(vm)
            vm_obj = VirtualMachine(client=ocp_admin_client, name=vm_name, namespace=vm_namespace)
            if vm_obj.exists:
                assert vm_obj.clean_up(wait=True), (
                    f"Failed to delete destination VM '{vm_name}' in namespace '{vm_namespace}'"
                )

        try:
            for sample in TimeoutSampler(
                wait_timeout=_ORPHAN_RESOURCE_WAIT_TIMEOUT,
                sleep=_ORPHAN_RESOURCE_POLL_INTERVAL,
                func=_get_orphan_resource_names,
                client=ocp_admin_client,
                namespace=vm_namespace,
            ):
                if not sample:
                    return
        except TimeoutExpiredError as exc:
            orphan_names = _get_orphan_resource_names(client=ocp_admin_client, namespace=vm_namespace)
            if not orphan_names:
                return
            raise AssertionError(
                f"Orphan resources remain in namespace '{vm_namespace}' after plan archive+delete: {orphan_names}"
            ) from exc
