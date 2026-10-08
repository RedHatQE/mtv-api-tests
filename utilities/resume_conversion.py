from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.exceptions import ExecOnPodError
from ocp_resources.migration import Migration
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from pytest_testconfig import py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import MigrationPlanExecError
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

_DISK_TRANSFER_STEP_NAME: str = "DiskTransfer"
_CONVERSION_POD_LABEL_SELECTOR: str = "forklift.app=virt-v2v"


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

    _wait_for_disk_transfer_complete(plan=plan)

    pre_failure_pvc_uids = verify_pvcs_bound(
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        migration_uid=migration_uid,
    )

    conversion_pod = _wait_for_conversion_pod(
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        migration_uid=migration_uid,
    )
    _kill_conversion_pod(pod=conversion_pod)

    return FailedConversionState(
        migration_uid=migration_uid,
        pvc_uids=pre_failure_pvc_uids,
    )


def _wait_for_disk_transfer_complete(plan: Plan) -> None:
    """Wait for the DiskTransfer pipeline step to complete.

    Args:
        plan (Plan): The Plan resource to monitor.

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
    target_namespace: str,
    migration_uid: str,
) -> Pod:
    """Wait for a running virt-v2v conversion pod.

    Polls for the virt-v2v pod owned by the initial Migration.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where migration pods run.
        migration_uid (str): UID of the initial Migration CR.

    Returns:
        Pod: The running conversion pod.

    Raises:
        TimeoutExpiredError: If no conversion pod appears within timeout.
    """
    LOGGER.info(f"Waiting for conversion pod ({_CONVERSION_POD_LABEL_SELECTOR})")
    for sample in TimeoutSampler(
        func=_find_conversion_pod,
        sleep=5,
        wait_timeout=py_config["plan_wait_timeout"],
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        migration_uid=migration_uid,
    ):
        if sample:
            LOGGER.info(f"Found running conversion pod '{sample.name}'")
            return sample


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
        label_selector=f"{_CONVERSION_POD_LABEL_SELECTOR},migration={migration_uid}",
    ):
        try:
            if pod.instance.status and pod.instance.status.phase == Pod.Status.RUNNING:
                return pod
        except NotFoundError:
            continue
    return None


def _kill_conversion_pod(pod: Pod) -> None:
    """Kill all processes in a conversion pod to simulate virt-v2v failure.

    Args:
        pod (Pod): The conversion pod to kill.
    """
    LOGGER.info(f"Killing processes in conversion pod '{pod.name}'")
    try:
        pod.execute(command=["sh", "-c", "kill -9 -1"], ignore_rc=True)
    except (ExecOnPodError, NotFoundError):
        pass  # Expected: kill -9 -1 terminates the exec shell, or pod already gone


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
        ValueError: If no PVCs found or a PVC has no status.
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
        pvc_uids[pvc.name] = pvc.instance.metadata.uid
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


def _get_migration_terminal_status(migration: Migration) -> str:
    """Return the active terminal condition type for a Migration.

    Args:
        migration (Migration): Migration CR to inspect.

    Returns:
        str: ``Succeeded`` or ``Failed`` when active, otherwise an empty string.
    """
    conditions = getattr(migration.instance.status, "conditions", []) or []
    for condition in conditions:
        if condition.status != migration.Condition.Status.TRUE:
            continue
        if condition.type in (Migration.Status.SUCCEEDED, Migration.Status.FAILED):
            return condition.type
    return ""


def _get_resume_failure_message(plan: Plan, migration_uid: str) -> str:
    """Get the detailed failure message for a resume Migration from Plan history.

    Args:
        plan (Plan): Plan containing migration history snapshots.
        migration_uid (str): UID of the resume Migration.

    Returns:
        str: Detailed failed-condition message, or a generic fallback.
    """
    history = getattr(plan.instance.status.migration, "history", []) or []
    for snapshot in reversed(history):
        snapshot_migration = getattr(snapshot, "migration", None)
        if getattr(snapshot_migration, "uid", None) != migration_uid:
            continue
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
        func=_get_migration_terminal_status,
        sleep=1,
        wait_timeout=py_config["plan_wait_timeout"],
        migration=migration,
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
