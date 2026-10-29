from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.exceptions import ExecOnPodError
from ocp_resources.migration import Migration
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.virtual_machine import VirtualMachine
from pytest_testconfig import py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import MigrationPlanExecError
from utilities.mtv_migration import get_plan_migration_status
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

_DISK_TRANSFER_STEP_NAME: str = "DiskTransfer"
CONVERSION_POD_LABEL_SELECTOR: str = "forklift.app=virt-v2v"

LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class FailedConversionState:
    """Identifiers captured before the initial conversion is interrupted.

    Args:
        migration_uid (str): UID used to identify the initial migration resources.
        pvc_uids (dict[str, str]): PVC names and UIDs captured after disk copy.
    """

    migration_uid: str
    pvc_uids: dict[str, str]


def start_migration_and_kill_conversion(
    ocp_admin_client: "DynamicClient",
    fixture_store: dict[str, Any],  # Any: pytest fixture store has a dynamic teardown structure.
    plan: Plan,
    target_namespace: str,
    cut_over: datetime,
    on_status_poll: Callable[[str], None] | None = None,
) -> FailedConversionState:
    """Start a warm migration, wait for disk transfer, record PVC UIDs, then kill the conversion pod.

    Creates a Migration CR, waits for the ``DiskTransfer`` pipeline step to
    complete, snapshots PVC UIDs (proving disk data is written), waits for the
    virt-v2v conversion pod to start, then kills all its processes.

    The caller should then call ``wait_for_migration_complate(plan)`` inside
    ``pytest.raises(MigrationPlanExecError, match="ImageConversion")`` to
    verify the migration failed at the correct step.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        fixture_store (dict[str, Any]): Fixture store for resource tracking.
        plan (Plan): The Plan CR for the migration.
        target_namespace (str): Target namespace for the Migration CR.
        cut_over (datetime): Cutover time for the warm migration.
        on_status_poll (Callable[[str], None] | None): Optional callback invoked while waiting for disk transfer.

    Returns:
        FailedConversionState: Migration and PVC identifiers recorded
        after disk transfer completed and before conversion was interrupted.

    Raises:
        ValueError: If the created Migration has no UID.
    """
    migration = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Migration,
        name=f"{plan.name}-initial",
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        cut_over=cut_over,
    )
    migration_uid = migration.instance.metadata.uid
    if not migration_uid:
        raise ValueError(f"Migration '{migration.name}' has no UID")

    _wait_for_disk_transfer_complete(plan=plan, on_status_poll=on_status_poll)

    pre_failure_pvc_uids = verify_pvcs_bound(
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        migration_uid=migration_uid,
    )

    conversion_pod = _wait_for_conversion_pod(
        ocp_admin_client=ocp_admin_client,
        plan=plan,
        target_namespace=target_namespace,
        migration_uid=migration_uid,
    )
    _kill_conversion_pod(
        pod=conversion_pod,
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
    )

    return FailedConversionState(
        migration_uid=migration_uid,
        pvc_uids=pre_failure_pvc_uids,
    )


def _wait_for_disk_transfer_complete(
    plan: Plan,
    on_status_poll: Callable[[str], None] | None,
) -> None:
    """Wait for the DiskTransfer pipeline step to complete.

    Args:
        plan (Plan): The Plan resource to monitor.
        on_status_poll (Callable[[str], None] | None): Optional callback invoked on each active migration poll.

    Raises:
        TimeoutExpiredError: If disk transfer does not complete within timeout.
    """
    LOGGER.info(f"Waiting for DiskTransfer to complete for plan '{plan.name}'")
    for sample in TimeoutSampler(
        func=_is_disk_transfer_complete,
        sleep=10,
        wait_timeout=py_config["plan_wait_timeout"],
        plan=plan,
    ):
        _raise_if_initial_migration_failed(plan=plan)
        if on_status_poll is not None:
            on_status_poll(Plan.Status.EXECUTING)
        if sample:
            return


def _is_disk_transfer_complete(plan: Plan) -> bool:
    """Check if the DiskTransfer pipeline step has completed.

    Args:
        plan (Plan): The Plan resource to check.

    Returns:
        bool: True if DiskTransfer is completed for all VMs.
    """
    try:
        vms = plan.instance.status.migration.vms
        if not vms:
            return False
        for vm in vms:
            pipeline = getattr(vm, "pipeline", None)
            if not pipeline:
                return False
            for step in pipeline:
                if step.name == _DISK_TRANSFER_STEP_NAME:
                    if step.phase != "Completed":
                        return False
                    break
            else:
                return False
    except (AttributeError, TypeError):
        return False

    LOGGER.info(f"DiskTransfer completed for plan '{plan.name}'")
    return True


def _wait_for_conversion_pod(
    ocp_admin_client: "DynamicClient",
    plan: Plan,
    target_namespace: str,
    migration_uid: str,
) -> Pod:
    """Wait for a running virt-v2v conversion pod.

    Polls for the virt-v2v pod owned by the initial Migration.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        plan (Plan): The initial migration Plan to monitor for failure.
        target_namespace (str): Namespace where migration pods run.
        migration_uid (str): UID of the initial Migration CR.

    Returns:
        Pod: The running conversion pod.

    Raises:
        TimeoutExpiredError: If no conversion pod appears within timeout.
    """
    LOGGER.info(f"Waiting for conversion pod ({CONVERSION_POD_LABEL_SELECTOR})")
    for sample in TimeoutSampler(
        func=_find_conversion_pod,
        sleep=5,
        wait_timeout=py_config["plan_wait_timeout"],
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        migration_uid=migration_uid,
    ):
        _raise_if_initial_migration_failed(plan=plan)
        if sample:
            LOGGER.info(f"Found running conversion pod '{sample.name}'")
            return sample


def _raise_if_initial_migration_failed(plan: Plan) -> None:
    """Raise immediately when the initial migration reaches a failed terminal state.

    Args:
        plan (Plan): Initial migration Plan to inspect.

    Raises:
        MigrationPlanExecError: If the initial migration has failed.
    """
    if get_plan_migration_status(plan=plan) == Plan.Status.FAILED:
        raise MigrationPlanExecError(f"Initial migration for plan '{plan.name}' failed. \nstatus:\n\t{plan.instance}")


def _find_conversion_pod(
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    migration_uid: str,
) -> Pod | None:
    """Find a running pod with the virt-v2v label.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace to search.
        migration_uid (str): UID used to select the conversion pod.

    Returns:
        Pod | None: A running conversion pod, or None if not found.
    """
    for pod in Pod.get(
        client=ocp_admin_client,
        namespace=target_namespace,
        label_selector=f"{CONVERSION_POD_LABEL_SELECTOR},migration={migration_uid}",
    ):
        try:
            if pod.instance.status and pod.instance.status.phase == Pod.Status.RUNNING:
                return pod
        except NotFoundError:
            continue
    return None


def _kill_conversion_pod(
    pod: Pod,
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
) -> None:
    """Kill all processes in a conversion pod to simulate virt-v2v failure.

    Args:
        pod (Pod): The conversion pod to kill.

        ocp_admin_client (DynamicClient): OpenShift admin client used to verify
            that the pod stopped.
        target_namespace (str): Namespace containing the conversion pod.

    Raises:
        TimeoutExpiredError: If the conversion pod remains running after the
            kill command.
    """
    LOGGER.info(f"Killing processes in conversion pod '{pod.name}'")
    # `kill -9 -1` cannot signal PID 1: the -1 broadcast excludes it, and a PID
    # namespace never delivers SIGKILL to its init process from inside. The explicit
    # SIGTERM covers PID 1 — unlike SIGKILL it is delivered when PID 1 has a handler
    # (catatonit forwards it to virt-v2v-wrapper; the Go runtime handles it when the
    # wrapper itself is PID 1), so the container terminates on every pod layout.
    try:
        pod.execute(command=["sh", "-c", "kill -9 -1; kill -TERM 1"], ignore_rc=True)
    except (ExecOnPodError, NotFoundError):
        LOGGER.info(f"Conversion pod '{pod.name}' stopped its exec session as expected")

    for stopped in TimeoutSampler(
        func=_is_conversion_pod_stopped,
        sleep=2,
        wait_timeout=py_config["plan_wait_timeout"],
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        pod_name=pod.name,
    ):
        if stopped:
            LOGGER.info(f"Verified conversion pod '{pod.name}' stopped")
            return


def _is_conversion_pod_stopped(
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    pod_name: str,
) -> bool:
    """Check whether a conversion pod disappeared or is no longer running.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace containing the conversion pod.
        pod_name (str): Name of the conversion pod.

    Returns:
        bool: True when the pod is gone or no longer in Running phase.
    """
    try:
        pods = list(
            Pod.get(
                client=ocp_admin_client,
                namespace=target_namespace,
                name=pod_name,
            )
        )
    except NotFoundError:
        return True
    if not pods:
        return True
    try:
        status = pods[0].instance.status
    except NotFoundError:
        return True
    return not status or status.phase != Pod.Status.RUNNING


def verify_pvcs_bound(
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    migration_uid: str,
) -> dict[str, str]:
    """Verify migration PVCs are Bound and return their UIDs.

    Gets PVCs labeled for the initial Migration and verifies each is in Bound phase.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace containing migration PVCs.
        migration_uid (str): Initial Migration UID used to select only its PVCs.

    Returns:
        dict[str, str]: Mapping of PVC name to UID.

    Raises:
        ValueError: If no PVCs are found, or a PVC has no status or UID.
        AssertionError: If any PVC is not in Bound phase.
    """
    pvcs = list(
        PersistentVolumeClaim.get(
            client=ocp_admin_client,
            namespace=target_namespace,
            label_selector=f"migration={migration_uid}",
        )
    )
    if not pvcs:
        raise ValueError(f"No PVCs found in namespace '{target_namespace}'")

    pvc_uids: dict[str, str] = {}
    for pvc in pvcs:
        status = pvc.instance.status
        if not status:
            raise ValueError(f"PVC '{pvc.name}' has no status")
        phase = status.phase
        assert phase == PersistentVolumeClaim.Status.BOUND, (
            f"PVC '{pvc.name}' is in phase '{phase}', expected '{PersistentVolumeClaim.Status.BOUND}'"
        )
        pvc_uid = pvc.instance.metadata.uid
        if not pvc_uid:
            raise ValueError(f"PVC '{pvc.name}' has no UID")
        pvc_uids[pvc.name] = pvc_uid
        LOGGER.info(f"PVC '{pvc.name}' is Bound (UID: {pvc_uids[pvc.name]})")

    return pvc_uids


def verify_resume_skipped_disk_copy(plan: Plan) -> None:
    """Verify the resumed migration did not re-execute disk transfer.

    Inspects the plan VM pipeline after a successful resume migration.
    The resume itinerary must omit DiskTransfer for every VM. The initial
    migration already proved that DiskTransfer completed before conversion.

    Args:
        plan (Plan): The Plan CR to inspect after resume.

    Raises:
        ValueError: If the plan has no VM migration status.
        AssertionError: If DiskTransfer was re-executed during resume.
    """
    vms = plan.instance.status.migration.vms
    if not vms:
        raise ValueError(f"Plan '{plan.name}' has no VM migration status")

    for vm in vms:
        pipeline = getattr(vm, "pipeline", [])
        if not pipeline:
            raise ValueError("A resumed VM has no migration pipeline")
        LOGGER.info(f"Resume pipeline for VM: {[{'name': s.name, 'phase': s.phase} for s in pipeline]}")
        step_names = [step.name for step in pipeline]
        assert _DISK_TRANSFER_STEP_NAME not in step_names, (
            f"Resume pipeline includes {_DISK_TRANSFER_STEP_NAME} for VM '{getattr(vm, 'name', 'unknown')}'"
        )

    LOGGER.info("Verified: No VM resume pipeline includes DiskTransfer")


def verify_resumed_vm_uses_preserved_pvcs(
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    vm_name: str,
    pvc_uids: dict[str, str],
) -> None:
    """Verify resumed destination VMs attach the PVCs preserved from the failed migration.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace containing destination VMs and PVCs.
        vm_name (str): Expected destination VM name.
        pvc_uids (dict[str, str]): Preserved PVC names and UIDs from the failed migration.

    Raises:
        ValueError: If the expected destination VM is absent or has no PVC volumes.
        AssertionError: If the destination VM does not attach exactly the preserved PVCs.
    """
    destination_vms = {vm.name: vm for vm in VirtualMachine.get(client=ocp_admin_client, namespace=target_namespace)}
    destination_vm = destination_vms.get(vm_name)
    if destination_vm is None:
        raise ValueError(f"Destination VM '{vm_name}' was not found")
    volumes = getattr(destination_vm.instance.spec.template.spec, "volumes", []) or []
    attached_pvc_names = {
        claim_name
        for volume in volumes
        if (pvc := getattr(volume, "persistentVolumeClaim", None)) and (claim_name := getattr(pvc, "claimName", None))
    }
    if not attached_pvc_names:
        raise ValueError(f"Destination VM '{vm_name}' has no PVC volumes")
    expected_pvc_names = set(pvc_uids)
    assert attached_pvc_names == expected_pvc_names, (
        f"Destination VM '{vm_name}' attaches PVCs {sorted(attached_pvc_names)}, "
        f"expected preserved PVCs {sorted(expected_pvc_names)}"
    )
    attached_pvc_uids: dict[str, str] = {}
    for pvc in PersistentVolumeClaim.get(client=ocp_admin_client, namespace=target_namespace):
        if pvc.name not in attached_pvc_names:
            continue
        pvc_uid = pvc.instance.metadata.uid
        if not pvc_uid:
            raise ValueError(f"Attached PVC '{pvc.name}' has no UID")
        attached_pvc_uids[pvc.name] = pvc_uid
    assert attached_pvc_uids == pvc_uids, (
        f"Destination VM '{vm_name}' attaches PVC UIDs {attached_pvc_uids}, expected preserved PVC UIDs {pvc_uids}"
    )


def verify_image_conversion_failed(plan: Plan) -> None:
    """Verify that the interrupted migration failed during ImageConversion.

    Args:
        plan (Plan): The Plan resource to inspect after migration failure.

    Raises:
        ValueError: If no VM pipeline contains a failed ImageConversion step.
    """
    vms = getattr(plan.instance.status.migration, "vms", []) or []
    if not vms:
        raise ValueError(f"Plan '{plan.name}' has no VM migration status")

    for vm in vms:
        pipeline = getattr(vm, "pipeline", []) or []
        for step in pipeline:
            if step.name != "ImageConversion":
                continue
            # A failed pipeline step is identified solely by its populated
            # `error` object — forklift step phases are only Started/Pending/
            # Running/Completed (no "Failed" step phase exists), so an
            # error-bearing step is the terminal failure state for that step.
            if getattr(step, "error", None):
                return

    raise ValueError(f"Plan '{plan.name}' did not fail during ImageConversion")


def _get_migration_terminal_status(migration: Migration) -> str:
    """Return the active terminal condition type for a Migration.

    Args:
        migration (Migration): Migration CR to inspect.

    Returns:
        str: ``Succeeded`` or ``Failed`` when active, otherwise an empty string.
    """
    migration_instance = getattr(migration, "instance", None)
    migration_status = getattr(migration_instance, "status", None)
    conditions = getattr(migration_status, "conditions", []) or []
    for condition in conditions:
        if condition.status != migration.Condition.Status.TRUE:
            continue
        if condition.type in (Migration.Status.SUCCEEDED, Migration.Status.FAILED):
            return condition.type
    return ""


def _get_migration_history_snapshot(plan: Plan, migration_uid: str) -> object | None:
    """Return the Plan history snapshot belonging to a Migration.

    Args:
        plan (Plan): Plan containing migration history snapshots.
        migration_uid (str): UID of the Migration to find.

    Returns:
        object | None: Matching history snapshot, or ``None`` when it is absent.
    """
    plan_instance = getattr(plan, "instance", None)
    plan_status = getattr(plan_instance, "status", None)
    migration_status = getattr(plan_status, "migration", None)
    history = getattr(migration_status, "history", []) or []
    for snapshot in reversed(history):
        snapshot_migration = getattr(snapshot, "migration", None)
        if getattr(snapshot_migration, "uid", None) == migration_uid:
            return snapshot
    return None


def _get_resume_plan_terminal_status(plan: Plan, migration_uid: str) -> str:
    """Return the terminal status recorded for the exact resume Migration in Plan history.

    Args:
        plan (Plan): Plan containing migration history snapshots.
        migration_uid (str): UID of the resume Migration.

    Returns:
        str: ``Succeeded`` or ``Failed`` when recorded, otherwise an empty string.
    """
    snapshot = _get_migration_history_snapshot(plan=plan, migration_uid=migration_uid)
    if snapshot is None:
        return ""
    for condition in getattr(snapshot, "conditions", []) or []:
        if condition.status != Migration.Condition.Status.TRUE:
            continue
        if condition.type in (Plan.Status.SUCCEEDED, Plan.Status.FAILED):
            return condition.type
    return ""


def _get_resume_terminal_status(migration: Migration, plan: Plan, migration_uid: str) -> str:
    """Return terminal status from the Migration, or its matching Plan history entry.

    Args:
        migration (Migration): Resume Migration resource.
        plan (Plan): Plan associated with the resume Migration.
        migration_uid (str): UID of the resume Migration.

    Returns:
        str: ``Succeeded`` or ``Failed`` when active, otherwise an empty string.
    """
    return _get_migration_terminal_status(migration=migration) or _get_resume_plan_terminal_status(
        plan=plan,
        migration_uid=migration_uid,
    )


def _get_resume_failure_message(plan: Plan, migration_uid: str) -> str:
    """Get the detailed failure message for a resume Migration from Plan history.

    Args:
        plan (Plan): Plan containing migration history snapshots.
        migration_uid (str): UID of the resume Migration.

    Returns:
        str: Detailed failed-condition message, or a generic fallback.
    """
    snapshot = _get_migration_history_snapshot(plan=plan, migration_uid=migration_uid)
    if snapshot is None:
        return "The resume migration failed"
    for condition in getattr(snapshot, "conditions", []) or []:
        if condition.type == Plan.Status.FAILED and condition.status == Migration.Condition.Status.TRUE:
            return getattr(condition, "message", None) or "The resume migration failed"
    return "The resume migration failed"


def _wait_for_resume_migration_complete(migration: Migration, plan: Plan) -> None:
    """Wait for the exact resume Migration CR to reach a terminal condition.

    Args:
        migration (Migration): Resume Migration resource to monitor.
        plan (Plan): Plan used to retrieve detailed failure conditions.

    Raises:
        MigrationPlanExecError: If Forklift rejects or fails the resume migration.
        TimeoutExpiredError: If the resume migration does not finish in time.
        ValueError: If the resume Migration has no UID.
    """
    migration_uid = migration.instance.metadata.uid
    if not migration_uid:
        raise ValueError(f"Migration '{migration.name}' has no UID")

    for status in TimeoutSampler(
        func=_get_resume_terminal_status,
        sleep=1,
        wait_timeout=py_config["plan_wait_timeout"],
        migration=migration,
        plan=plan,
        migration_uid=migration_uid,
    ):
        if status == Migration.Status.SUCCEEDED:
            return
        if status == Migration.Status.FAILED:
            message = _get_resume_failure_message(plan=plan, migration_uid=migration_uid)
            raise MigrationPlanExecError(f"Resume migration for plan '{plan.name}' failed: {message}")


def execute_resume_migration(
    ocp_admin_client: "DynamicClient",
    fixture_store: dict[str, Any],  # Any: pytest fixture store has a dynamic teardown structure.
    plan: Plan,
    target_namespace: str,
) -> None:
    """Create a resume Migration CR and wait for completion.

    Creates a Migration CR with ``resumeConversion: true``, which skips disk
    copy and re-runs only the virt-v2v conversion phase using preserved PVCs.
    Forklift handles cleanup of failed conversion pods automatically.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        fixture_store (dict[str, Any]): Fixture store for resource tracking.
        plan (Plan): The Plan CR to resume.
        target_namespace (str): Target namespace for the Migration CR.

    Raises:
        MigrationPlanExecError: If Forklift rejects or fails the resume migration.
        TimeoutExpiredError: If the resume migration does not finish in time.
    """
    resume_name = f"{plan.name}-resume"[:63]
    migration = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Migration,
        name=resume_name,
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        resume_conversion=True,
    )
    _wait_for_resume_migration_complete(migration=migration, plan=plan)
