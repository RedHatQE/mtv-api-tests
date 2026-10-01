from __future__ import annotations

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
from utilities.mtv_migration import get_plan_migration_status
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)

_DISK_TRANSFER_STEP_NAME: str = "DiskTransfer"
_CONVERSION_POD_LABEL_SELECTOR: str = "forklift.app=virt-v2v"


def start_migration_and_kill_conversion(
    ocp_admin_client: "DynamicClient",
    fixture_store: dict[str, Any],
    plan: Plan,
    target_namespace: str,
    cut_over: datetime,
) -> dict[str, str]:
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
        dict[str, str]: PVC name-to-UID mapping recorded after disk transfer
        completed (before the conversion pod was killed).
    """
    create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Migration,
        name=f"{plan.name}-initial",
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        cut_over=cut_over,
    )
    _wait_for_disk_transfer_complete(plan=plan)

    pre_failure_pvc_uids = verify_pvcs_bound(
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
    )

    conversion_pod = _wait_for_conversion_pod(
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
    )
    _kill_conversion_pod(pod=conversion_pod)

    return pre_failure_pvc_uids


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
) -> Pod:
    """Wait for a running virt-v2v conversion pod.

    Polls for pods with the ``forklift.app=virt-v2v`` label in the target namespace.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where migration pods run.

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
    ):
        if sample:
            LOGGER.info(f"Found running conversion pod '{sample.name}'")
            return sample


def _find_conversion_pod(
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
) -> Pod | None:
    """Find a running pod with the virt-v2v label.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace to search.

    Returns:
        Pod | None: A running conversion pod, or None if not found.
    """
    for pod in Pod.get(
        client=ocp_admin_client,
        namespace=target_namespace,
        label_selector=_CONVERSION_POD_LABEL_SELECTOR,
    ):
        try:
            if pod.instance.status and pod.instance.status.phase == "Running":
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
) -> dict[str, str]:
    """Verify migration PVCs are Bound and return their UIDs.

    Gets all PVCs in the target namespace (unique per test session) and
    verifies each is in Bound phase.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace containing migration PVCs.

    Returns:
        dict[str, str]: Mapping of PVC name to UID.

    Raises:
        ValueError: If no PVCs found or a PVC has no status.
        AssertionError: If any PVC is not in Bound phase.
    """
    pvcs = list(PersistentVolumeClaim.get(client=ocp_admin_client, namespace=target_namespace))
    if not pvcs:
        raise ValueError(f"No PVCs found in namespace '{target_namespace}'")

    pvc_uids: dict[str, str] = {}
    for pvc in pvcs:
        status = pvc.instance.status
        if not status:
            raise ValueError(f"PVC '{pvc.name}' has no status")
        phase = status.phase
        assert phase == "Bound", f"PVC '{pvc.name}' is in phase '{phase}', expected 'Bound'"
        pvc_uids[pvc.name] = pvc.instance.metadata.uid
        LOGGER.info(f"PVC '{pvc.name}' is Bound (UID: {pvc_uids[pvc.name]})")

    return pvc_uids


def verify_resume_skipped_disk_copy(plan: Plan) -> None:
    """Verify the resumed migration did not re-execute disk transfer.

    Inspects the plan VM pipeline after a successful resume migration.
    The DiskTransfer step must not have been re-run — it should either be
    absent or already Completed from the original migration.

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
        LOGGER.info(f"Resume pipeline for VM: {[{'name': s.name, 'phase': s.phase} for s in pipeline]}")
        for step in pipeline:
            if step.name == _DISK_TRANSFER_STEP_NAME:
                assert step.phase == "Completed", (
                    f"DiskTransfer was re-executed during resume (phase: {step.phase}), "
                    f"expected Completed from original migration"
                )
                LOGGER.info("Verified: DiskTransfer not re-executed during resume")
                return

    LOGGER.info("Verified: No DiskTransfer step in resume pipeline (skipped entirely)")


def _wait_for_resume_migration_complete(plan: Plan) -> None:
    """Wait for a resume migration to complete, handling stale Plan FAILED status.

    After a first migration failure, the Plan's FAILED condition remains True
    until Forklift reconciles the new resume Migration CR. This function skips
    initial FAILED statuses and waits for the plan to transition through
    Executing to Succeeded.

    Args:
        plan (Plan): The Plan resource to monitor.

    Raises:
        MigrationPlanExecError: If resume migration fails or times out.
    """
    seen_non_failed = False
    last_status: str = ""

    try:
        for sample in TimeoutSampler(
            func=get_plan_migration_status,
            sleep=1,
            wait_timeout=py_config["plan_wait_timeout"],
            plan=plan,
        ):
            if sample != last_status:
                LOGGER.info(f"Plan '{plan.name}' resume migration status: '{sample}'")
                last_status = sample

            if sample == Plan.Status.FAILED and not seen_non_failed:
                continue

            if sample == Plan.Status.EXECUTING:
                seen_non_failed = True

            if sample == Plan.Status.SUCCEEDED:
                return

            if sample == Plan.Status.FAILED and seen_non_failed:
                raise MigrationPlanExecError()

    except (TimeoutExpiredError, MigrationPlanExecError):
        raise MigrationPlanExecError(f"Resume migration for plan '{plan.name}' failed.\nstatus:\n\t{plan.instance}")


def execute_resume_migration(
    ocp_admin_client: "DynamicClient",
    fixture_store: dict[str, Any],
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
        MigrationPlanExecError: If the resume migration fails.
    """
    resume_name = f"{plan.name}-resume"[:63]
    create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Migration,
        name=resume_name,
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        resume_conversion=True,
    )
    _wait_for_resume_migration_complete(plan=plan)
