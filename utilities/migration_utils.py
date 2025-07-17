import contextlib
from datetime import datetime, timedelta
from typing import Any

import pytz
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.datavolume import DataVolume
from ocp_resources.migration import Migration
from ocp_resources.network_map import NetworkMap
from ocp_resources.persistent_volume import PersistentVolume
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import NamespacedResource, Resource, ResourceEditor
from ocp_resources.storage_map import StorageMap
from pytest import FixtureRequest
from pytest_testconfig import py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError

from libs.base_provider import BaseProvider
from libs.providers.openshift import OCPProvider
from utilities.utils import generate_name_with_uuid, get_value_from_py_config

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


def prepare_migration_for_tests(
    plan: dict[str, Any],
    warm_migration: bool,
    request: FixtureRequest,
    source_provider: BaseProvider,
    destination_provider: OCPProvider,
    network_migration_map: NetworkMap,
    storage_migration_map: StorageMap,
    target_namespace: str,
    fixture_store: Any,
    source_vms_namespace: str,
    cut_over: datetime | None = None,
    pre_hook_name: str | None = None,
    pre_hook_namespace: str | None = None,
    after_hook_name: str | None = None,
    after_hook_namespace: str | None = None,
) -> dict[str, Any]:
    test_name = request._pyfuncitem.name
    _source_provider_type = py_config.get("source_provider_type")

    _plan_name = (
        f"{target_namespace}{'-remote' if get_value_from_py_config('remote_ocp_cluster') else ''}"
        f"-{'warm' if warm_migration else 'cold'}"
    )

    plan_name = generate_name_with_uuid(name=_plan_name)

    # Plan CR accepts only VM name/id
    virtual_machines_list: list[dict[str, str]] = [{"name": vm["name"]} for vm in plan["virtual_machines"]]

    if _source_provider_type == Provider.ProviderType.OPENSHIFT:
        for idx in range(len(virtual_machines_list)):
            virtual_machines_list[idx].update({"namespace": source_vms_namespace})

    return {
        "name": plan_name,
        "source_provider_name": source_provider.ocp_resource.name,
        "source_provider_namespace": source_provider.ocp_resource.namespace,
        "virtual_machines_list": virtual_machines_list,
        "warm_migration": warm_migration,
        "network_map_name": network_migration_map.name,
        "network_map_namespace": network_migration_map.namespace,
        "storage_map_name": storage_migration_map.name,
        "storage_map_namespace": storage_migration_map.namespace,
        "target_namespace": target_namespace,
        "pre_hook_name": pre_hook_name,
        "pre_hook_namespace": pre_hook_namespace,
        "after_hook_name": after_hook_name,
        "after_hook_namespace": after_hook_namespace,
        "cut_over": cut_over,
        "destination_provider_name": destination_provider.ocp_resource.name,
        "destination_provider_namespace": destination_provider.ocp_resource.namespace,
        "fixture_store": fixture_store,
        "test_name": test_name,
    }


def get_cutover_value(current_cutover: bool = False) -> datetime:
    datetime_utc = datetime.now(pytz.utc)
    if current_cutover:
        return datetime_utc

    return datetime_utc + timedelta(minutes=int(py_config["mins_before_cutover"]))
