"""MTV-5663: Check PVC cleanup after a PostHook-failed migration Plan.

This exercises Plan archive/delete cleanup after a supported PostHook failure.
It complements MTV-5564 but does not reproduce its interrupted-transfer
prime-PVC leak: prime PVCs may disappear before PostHook failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import MigrationPlanExecError
from utilities.hooks import validate_hook_failure_and_check_vms
from utilities.migration_utils import archive_plan, get_orphan_resource_names
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_migration_for_plan,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.resources import unregister_teardown_resource
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider


_ORPHAN_RESOURCE_WAIT_TIMEOUT = 120  # Seconds to wait for async DV/PVC garbage collection
_ORPHAN_RESOURCE_POLL_INTERVAL = 5  # Seconds between polls


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
@pytest.mark.usefixtures("cleanup_migrated_vms", "plan_archive_vm_namespace")
class TestPlanArchivePvcCleanup:
    """Verify failed-Plan archive and deletion clean up destination DVs and PVCs.

    Purpose/Regression:
        Check Plan archive/delete cleanup after a migration fails at PostHook.
        This is complementary coverage for MTV-5564, not a reproduction of
        its mid-transfer prime-PVC leak. Prime PVCs may disappear before
        PostHook failure, so this scenario does not require one to remain.

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
        4. Just before archiving, confirm at least one PVC or DataVolume
           remains in the isolated effective VM target namespace.
        5. Archive the failed Plan. Check its Archived condition is True, then
           delete the Plan and confirm both the Plan and its Migration are gone.
        6. Before deleting any destination VM, poll that namespace for up to
           120 seconds until no DataVolumes or PVCs remain. After the check,
           class teardown removes retained destination VMs from the dedicated
           VM namespace; session teardown removes the disposable source VM,
           maps, Hook, dedicated namespace, and any source network created
           in step 1. The Plan and maps live in the session namespace.

    Expected result:
        1. The migration fails at PostHook with at least one PVC or DataVolume
           still present immediately before archiving.
        2. The failed Plan reaches Archived=True; deleting it also removes its
           Migration. If the Migration remains, report its name and namespace.
        3. Before destination VM teardown, no DataVolumes or PVCs remain in
           the isolated effective target namespace within 120 seconds. If a
           check fails, inspect the Plan conditions and the VM's failed step
           for PostHook, review the failing Hook's events or logs, then list
           remaining PVC and DataVolume names in the effective VM target namespace.
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
            vm_target_namespace=prepared_plan["_vm_target_namespace"],
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

        The post-hook triggers a failure after disk resources are created.
        The archive step checks whether any remain; a prime PVC may already
        have disappeared.

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

    def test_archive_and_delete_plan(
        self,
        fixture_store: dict[str, Any],  # Any: pytest fixture_store has dynamic teardown structure
        prepared_plan: dict[str, Any],  # Any: dynamic pytest plan config
        ocp_admin_client: DynamicClient,
    ) -> None:
        """Check remaining disk resources, then archive and delete the failed Plan.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            prepared_plan (dict[str, Any]): The prepared migration plan.
            ocp_admin_client (DynamicClient): OpenShift admin client.

        Returns:
            None

        Raises:
            AssertionError: If no disk resources remain, or Plan/Migration cleanup fails.
        """
        plan = self.__class__.plan_resource
        migration = get_migration_for_plan(plan)

        # The dedicated VM namespace contains only this migration's resources.
        # PVC names may be source disk UUIDs, so check without name filtering.
        # Prime PVC garbage collection may finish before the failed migration is observed;
        # either a remaining PVC or DV proves disk resources existed before archive.
        vm_namespace = prepared_plan["_vm_target_namespace"]
        assert get_orphan_resource_names(client=ocp_admin_client, namespace=vm_namespace), (
            f"No PVCs or DVs remain before archiving failed Plan in namespace '{vm_namespace}'"
        )
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
    ) -> None:
        """Verify all DVs and PVCs are gone before destination VM teardown.

        Poll for up to 120s after Plan archive and deletion because DV/PVC
        garbage collection is async. Class-scoped teardown handles retained VMs.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            ocp_admin_client (DynamicClient): OpenShift admin client.

        Returns:
            None

        Raises:
            AssertionError: If orphan resources remain after 120s timeout.
        """
        vm_namespace = prepared_plan["_vm_target_namespace"]
        try:
            for sample in TimeoutSampler(
                wait_timeout=_ORPHAN_RESOURCE_WAIT_TIMEOUT,
                sleep=_ORPHAN_RESOURCE_POLL_INTERVAL,
                func=get_orphan_resource_names,
                client=ocp_admin_client,
                namespace=vm_namespace,
            ):
                if not sample:
                    return
        except TimeoutExpiredError as exc:
            orphan_names = get_orphan_resource_names(client=ocp_admin_client, namespace=vm_namespace)
            if not orphan_names:
                return
            raise AssertionError(
                f"Orphan resources remain in namespace '{vm_namespace}' after plan archive+delete: {orphan_names}"
            ) from exc
