import contextlib

from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.datavolume import DataVolume
from ocp_resources.migration import Migration
from ocp_resources.persistent_volume import PersistentVolume
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.resource import NamespacedResource, Resource, ResourceEditor
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError

LOGGER = get_logger(__name__)


def cancel_migration(migration: Migration) -> None:
    for condition in migration.instance.status.conditions:
        # Only cancel migrations that are in "Executing" state
        if condition.type == migration.Condition.Type.RUNNING and condition.status == migration.Condition.Status.TRUE:
            LOGGER.info(f"Canceling migration {migration.name}")

            migration_spec = migration.instance.spec
            plan = Plan(client=migration.client, name=migration_spec.plan.name, namespace=migration_spec.plan.namespace)
            plan_instance = plan.instance

            ResourceEditor(
                patches={
                    migration: {
                        "spec": {
                            "cancel": plan_instance.spec.vms,
                        }
                    }
                }
            ).update()

            try:
                migration.wait_for_condition(
                    condition=migration.Condition.CANCELED, status=migration.Condition.Status.TRUE
                )
                check_dv_pvc_pv_deleted(
                    ocp_client=migration.client,
                    target_namespace=plan.instance.spec.targetNamespace,
                    partial_name=migration.name,
                )
            except TimeoutExpiredError:
                LOGGER.error(f"Failed to cancel migration {migration.name}")
                raise


def archive_plan(plan: Plan) -> None:
    LOGGER.info(f"Archiving plan {plan.name}")

    ResourceEditor(
        patches={
            plan: {
                "spec": {
                    "archived": True,
                }
            }
        }
    ).update()

    try:
        plan.wait_for_condition(condition=plan.Condition.ARCHIVED, status=plan.Condition.Status.TRUE)
        for _pod in Pod.get(dyn_client=plan.client, namespace=plan.instance.spec.targetNamespace):
            if plan.name in _pod.name:
                if not _pod.wait_deleted():
                    LOGGER.error(f"Pod {_pod.name} was not deleted after plan {plan.name} was archived")

    except TimeoutExpiredError:
        LOGGER.error(f"Failed to archive plan {plan.name}")
        raise


def check_dv_pvc_pv_deleted(
    ocp_client: DynamicClient,
    target_namespace: str,
    partial_name: str,
    leftovers: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    # When migration in not canceled (succeeded) DVs,PVCs are deleted only when the target_namespace is deleted
    # All calls wrap with `with contextlib.suppress(NotFoundError)` since the resources can be gone even after we get it.
    for _dv in DataVolume.get(dyn_client=ocp_client, namespace=target_namespace):
        with contextlib.suppress(NotFoundError):
            if partial_name in _dv.name:
                if not _dv.wait_deleted():
                    if leftovers:
                        leftovers = append_leftovers(leftovers=leftovers, resource=_dv)

    for _pvc in PersistentVolumeClaim.get(dyn_client=ocp_client, namespace=target_namespace):
        with contextlib.suppress(NotFoundError):
            if partial_name in _pvc.name:
                if not _pvc.wait_deleted():
                    if leftovers:
                        leftovers = append_leftovers(leftovers=leftovers, resource=_pvc)

    for _pv in PersistentVolume.get(dyn_client=ocp_client):
        with contextlib.suppress(NotFoundError):
            _pv_spec = _pv.instance.spec.to_dict()
            if partial_name in _pv_spec.get("claimRef", {}).get("name", ""):
                if _pv.instance.status.phase == _pv.Status.RELEASED:
                    continue

                if not _pv.wait_deleted():
                    if leftovers:
                        leftovers = append_leftovers(leftovers=leftovers, resource=_pv)

    return leftovers or {}


def append_leftovers(
    leftovers: dict[str, list[dict[str, str]]], resource: Resource | NamespacedResource
) -> dict[str, list[dict[str, str]]]:
    _name = resource.name
    _namespace = resource.namespace

    leftovers.setdefault(resource.kind, []).append({
        "name": _name,
        "namespace": _namespace,
    })

    return leftovers
