"""Shared disk remigration tests for RHEL VMs (MTV-7353).

Covers remigration of a shared-disk owner VM after its destination VM was
deleted with orphan propagation, and the resulting duplicate shared-disk
PVC validation on subsequent plans (forklift PRs #7503 and #9182).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.resource import ResourceEditor
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine import VirtualMachine
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutSampler

from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.naming import resolve_destination_vm_name
from utilities.shared_disk import (
    get_vm_pvc_claim_names,
    get_shared_disk_pvc_names,
    get_shared_disk_source,
    verify_vms_share_single_pvc,
)
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider

LOGGER = get_logger(name=__name__)

_PLAN_CONDITION_TIMEOUT = 600
_PLAN_CONDITION_POLL_INTERVAL = 10
_VM_DELETE_TIMEOUT = 180

_DUPLICATE_WARNING_SUBSTRING = "duplicated"
_MULTIPLE_SHARED_PVCS_SUBSTRING = "Multiple shared disks PVCs"
_SHARED_WARN_DISKS_CONDITION_PREFIX = "SharedWarnDisks"
_SHARED_DISKS_CONDITION_PREFIX = "SharedDisks"


def _get_plan_conditions(plan: Plan) -> list[dict[str, Any]]:
    """Return the Plan's status conditions as plain dicts.

    Args:
        plan (Plan): Plan whose conditions are read.

    Returns:
        list[dict[str, Any]]: Condition dicts, empty when the Plan reports none.
    """
    status = plan.instance.status
    if not status:
        return []
    return [dict(cond) for cond in (status.conditions or [])]


def _find_plan_condition(
    conditions: list[dict[str, Any]],
    type_prefix: str,
    message_substring: str,
) -> dict[str, Any] | None:
    """Find a Plan condition by type prefix and message substring.

    Forklift suffixes shared-disk condition types with the source VM ID
    (e.g. ``SharedWarnDisks-vm-123``), so the type is matched by prefix.

    Args:
        conditions (list[dict[str, Any]]): Plan status conditions.
        type_prefix (str): Condition type prefix to match (e.g. ``SharedDisks``).
        message_substring (str): Substring required in the condition message.

    Returns:
        dict[str, Any] | None: The matching condition, or None when absent.
    """
    for cond in conditions:
        if str(cond.get("type", "")).startswith(type_prefix) and message_substring in str(cond.get("message", "")):
            return cond
    return None


def _plan_is_ready(conditions: list[dict[str, Any]]) -> bool:
    """Report whether the conditions carry a Ready=True condition.

    Args:
        conditions (list[dict[str, Any]]): Plan status conditions.

    Returns:
        bool: True when a Ready condition with status True is present.
    """
    return any(
        cond.get("type") == Plan.Condition.READY and cond.get("status") == Plan.Condition.Status.TRUE
        for cond in conditions
    )


@pytest.mark.vsphere
@pytest.mark.shared_disk
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_shared_disk_remigrate"],
        )
    ],
    indirect=True,
    ids=["shared-disk-remigrate"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestSharedDiskRemigrate:
    """Migrate shared-disk VMs, orphan-delete the owner, remigrate it, and verify the duplicate-PVC handling (MTV-7353).

    Purpose/Regression:
        After an initial shared-disk migration (MTV-6076), deleting the
        destination owner VM with orphan propagation must leave the shared
        disk PVC in place (ownerReferences stripped). Re-migrating the owner
        VM must succeed while surfacing a duplicate shared-disk warning
        (forklift PR #7503), leaving two shared-disk PVCs on the
        destination. A subsequent consumer-only plan must then be blocked
        from becoming Ready by the multiple-shared-disks-PVCs validation
        (forklift PR #9182). There is no induced failure.

    Prerequisites:
        Register and connect a vSphere source provider and an OpenShift
        destination with MTV installed and shared-disk support, with
        accessible destination storage and network. Use two disposable
        Linux VMs that share one additional disk (a multi-writer VMDK),
        powered off before migration. The shared disk must be formatted
        with a filesystem. Have permission to create maps and Plans in a
        target namespace, and use a disposable environment.

    Test plan:
        1. Prepare the two VMs sharing the disk and wait until both appear
           in Forklift inventory.
        2. Create a StorageMap covering both VMs' disks and a NetworkMap
           for their networks. Confirm both are created.
        3. Create a cold Plan covering both VMs with the shared-disk
           setting enabled for the owner VM and disabled for the consumer
           VM, and target power state on.
        4. Run the migration and check it reaches Succeeded for both VMs.
           Verify both destination VMs share a single shared-disk PVC.
        5. Delete the destination owner VM with orphan propagation. If it
           hangs Terminating on the orphan finalizer, remove the finalizer
           via a patch and wait for deletion. Verify the shared PVC still
           exists, is Bound, and no longer has ownerReferences.
        6. Create a second Plan for the owner VM only (shared disks
           enabled) and run its migration. Verify it reaches Succeeded,
           that the Plan surfaces a duplicate shared-disk warning whose
           message mentions the duplicated disk, and that two shared-disk
           PVCs now exist for the same source disk.
        7. Create a third Plan for the consumer VM only (shared disks
           disabled) and do not run a migration. Poll the Plan conditions
           for a shared-disks blocker reporting multiple shared-disk PVCs,
           and verify the Plan does not have a Ready=True condition.

    Expected result:
        The initial migration succeeds with one shared PVC, the orphaned
        owner deletion preserves the PVC, the owner remigration succeeds
        with a duplicate-PVC warning and a second PVC, and the consumer
        plan is blocked by the multiple-shared-disks-PVCs validation. On
        failure, inspect the Plan and Migration CR conditions, the
        shared-disk PVCs (shareable label and disk-source annotation) in
        the target namespace, and the destination VM deletion and
        finalizer state.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_initial: Plan
    plan_recreate: Plan
    plan_consumer: Plan
    shared_pvc_name: str
    shared_disk_source: str

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
        """Create StorageMap resource for both VMs."""
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
        """Create NetworkMap resource for both VMs."""
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
        """Create the initial MTV Plan CR with VM-level migrateSharedDisks overrides."""
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_initial = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan["warm_migration"],
            migrate_shared_disks=prepared_plan["migrate_shared_disks"],
            target_power_state=prepared_plan["target_power_state"],
        )
        assert self.plan_initial, "Plan creation failed"

    def test_migrate_vms(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute the initial migration and verify both VMs share a single shared-disk PVC."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_initial,
            target_namespace=target_namespace,
        )
        vm_namespace = prepared_plan["_vm_target_namespace"]
        vm1_dest_name = resolve_destination_vm_name(prepared_plan["virtual_machines"][0])
        vm2_dest_name = resolve_destination_vm_name(prepared_plan["virtual_machines"][1])
        verify_vms_share_single_pvc(
            ocp_admin_client=ocp_admin_client,
            target_namespace=vm_namespace,
            vm1_dest_name=vm1_dest_name,
            vm2_dest_name=vm2_dest_name,
        )
        vm1_pvc_names = get_vm_pvc_claim_names(ocp_admin_client, vm_namespace, vm1_dest_name)
        vm2_pvc_names = get_vm_pvc_claim_names(ocp_admin_client, vm_namespace, vm2_dest_name)
        (self.__class__.shared_pvc_name,) = vm1_pvc_names & vm2_pvc_names
        self.__class__.shared_disk_source = get_shared_disk_source(
            ocp_admin_client=ocp_admin_client,
            namespace=vm_namespace,
            pvc_name=self.shared_pvc_name,
        )
        LOGGER.info(f"Shared PVC '{self.shared_pvc_name}' (disk source '{self.shared_disk_source}')")

    def test_delete_owner_vm_orphan(
        self,
        prepared_plan: dict[str, Any],
        ocp_admin_client: "DynamicClient",
    ) -> None:
        """Delete the destination owner VM with orphan propagation and verify the shared PVC survives."""
        vm_namespace = prepared_plan["_vm_target_namespace"]
        vm1_dest_name = resolve_destination_vm_name(prepared_plan["virtual_machines"][0])
        vm1_dest = VirtualMachine(
            client=ocp_admin_client,
            name=vm1_dest_name,
            namespace=vm_namespace,
            ensure_exists=True,
        )
        LOGGER.info(f"Deleting destination owner VM '{vm1_dest_name}' with orphan propagation")
        vm1_dest.delete(body={"propagationPolicy": "Orphan"})
        if not vm1_dest.wait_deleted(timeout=_VM_DELETE_TIMEOUT):
            LOGGER.warning(f"VM '{vm1_dest_name}' stuck Terminating; removing finalizers")
            ResourceEditor(
                patches={vm1_dest: {"metadata": {"finalizers": None}}},
            ).update()
            assert vm1_dest.wait_deleted(timeout=_VM_DELETE_TIMEOUT), (
                f"VM '{vm1_dest_name}' still Terminating after finalizer removal"
            )

        shared_pvc = PersistentVolumeClaim(
            client=ocp_admin_client,
            name=self.shared_pvc_name,
            namespace=vm_namespace,
            ensure_exists=True,
        )
        assert shared_pvc.exists, f"Shared PVC '{self.shared_pvc_name}' was deleted with the owner VM"
        assert shared_pvc.status == PersistentVolumeClaim.Status.BOUND, (
            f"Shared PVC '{self.shared_pvc_name}' is not Bound: {shared_pvc.status}"
        )
        assert not shared_pvc.instance.metadata.ownerReferences, (
            f"Shared PVC '{self.shared_pvc_name}' still has ownerReferences: "
            f"{shared_pvc.instance.metadata.ownerReferences}"
        )

    def test_remigrate_owner(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        target_namespace: str,
    ) -> None:
        """Re-migrate the owner VM and verify the duplicate shared-disk PVC warning."""
        vm_namespace = prepared_plan["_vm_target_namespace"]
        self.__class__.plan_recreate = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=[prepared_plan["virtual_machines"][0]],
            target_namespace=target_namespace,
            warm_migration=prepared_plan["warm_migration"],
            migrate_shared_disks=prepared_plan["migrate_shared_disks"],
            target_power_state=prepared_plan["target_power_state"],
        )
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_recreate,
            target_namespace=target_namespace,
        )
        for sample in TimeoutSampler(
            wait_timeout=_PLAN_CONDITION_TIMEOUT,
            sleep=_PLAN_CONDITION_POLL_INTERVAL,
            func=lambda: _find_plan_condition(
                conditions=_get_plan_conditions(self.plan_recreate),
                type_prefix=_SHARED_WARN_DISKS_CONDITION_PREFIX,
                message_substring=_DUPLICATE_WARNING_SUBSTRING,
            ),
        ):
            if sample:
                LOGGER.info(f"Duplicate shared-disk warning on Plan '{self.plan_recreate.name}': {sample['message']}")
                break

        pvc_names = get_shared_disk_pvc_names(
            ocp_admin_client=ocp_admin_client,
            namespace=vm_namespace,
            disk_source=self.shared_disk_source,
        )
        assert len(pvc_names) == 2, (
            f"Expected 2 shared-disk PVCs for source '{self.shared_disk_source}' after remigration, found: {pvc_names}"
        )

    def test_consumer_blocked(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        target_namespace: str,
    ) -> None:
        """Create the consumer-only plan and verify the duplicate-PVC blocker prevents readiness."""
        self.__class__.plan_consumer = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=[prepared_plan["virtual_machines"][1]],
            target_namespace=target_namespace,
            warm_migration=prepared_plan["warm_migration"],
            migrate_shared_disks=prepared_plan["migrate_shared_disks"],
            target_power_state=prepared_plan["target_power_state"],
            wait_for_ready=False,
        )
        for sample in TimeoutSampler(
            wait_timeout=_PLAN_CONDITION_TIMEOUT,
            sleep=_PLAN_CONDITION_POLL_INTERVAL,
            func=lambda: _find_plan_condition(
                conditions=_get_plan_conditions(self.plan_consumer),
                type_prefix=_SHARED_DISKS_CONDITION_PREFIX,
                message_substring=_MULTIPLE_SHARED_PVCS_SUBSTRING,
            ),
        ):
            if sample:
                LOGGER.info(f"Shared-disks blocker on Plan '{self.plan_consumer.name}': {sample['message']}")
                break

        conditions = _get_plan_conditions(self.plan_consumer)
        assert not _plan_is_ready(conditions=conditions), (
            f"Consumer Plan '{self.plan_consumer.name}' is Ready despite the shared-disks blocker: {conditions}"
        )
