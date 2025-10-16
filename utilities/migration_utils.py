import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def check_dv_pvc_pv_deleted(
    ocp_client: DynamicClient,
    target_namespace: str,
    partial_name: str,
    leftovers: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """
    Check and wait for DataVolumes, PVCs, and PVs to be deleted in parallel.
    Order is maintained: DVs → PVCs → PVs, but within each group resources are checked in parallel.
    """
    if leftovers is None:
        leftovers = {}

    def wait_for_resource_deletion(resource, resource_type):
        """Helper function to wait for a single resource deletion."""
        try:
            with contextlib.suppress(NotFoundError):
                if not resource.wait_deleted():
                    return {"success": False, "resource": resource, "type": resource_type}
            return {"success": True, "resource": resource, "type": resource_type}
        except Exception as exc:
            LOGGER.error(f"Failed to wait for {resource_type} {resource.name} deletion: {exc}")
            return {"success": False, "resource": resource, "type": resource_type}

    # Check DataVolumes in parallel
    dvs_to_wait = []
    try:
        dvs_to_wait = [
            _dv for _dv in DataVolume.get(dyn_client=ocp_client, namespace=target_namespace) if partial_name in _dv.name
        ]
    except Exception as exc:
        LOGGER.error(f"Failed to get DataVolumes: {exc}")

    if dvs_to_wait:
        LOGGER.info(f"Waiting for {len(dvs_to_wait)} DataVolumes to be deleted in parallel...")
        with ThreadPoolExecutor(max_workers=min(len(dvs_to_wait), 10)) as executor:
            future_to_dv = {executor.submit(wait_for_resource_deletion, dv, "DataVolume"): dv for dv in dvs_to_wait}
            for future in as_completed(future_to_dv):
                result = future.result()
                if not result["success"]:
                    leftovers = append_leftovers(leftovers=leftovers, resource=result["resource"])

    # Check PVCs in parallel
    pvcs_to_wait = []
    try:
        pvcs_to_wait = [
            _pvc
            for _pvc in PersistentVolumeClaim.get(dyn_client=ocp_client, namespace=target_namespace)
            if partial_name in _pvc.name
        ]
    except Exception as exc:
        LOGGER.error(f"Failed to get PVCs: {exc}")

    if pvcs_to_wait:
        LOGGER.info(f"Waiting for {len(pvcs_to_wait)} PVCs to be deleted in parallel...")
        with ThreadPoolExecutor(max_workers=min(len(pvcs_to_wait), 10)) as executor:
            future_to_pvc = {executor.submit(wait_for_resource_deletion, pvc, "PVC"): pvc for pvc in pvcs_to_wait}
            for future in as_completed(future_to_pvc):
                result = future.result()
                if not result["success"]:
                    leftovers = append_leftovers(leftovers=leftovers, resource=result["resource"])

    # Check PVs in parallel
    pvs_to_wait = []
    try:
        for _pv in PersistentVolume.get(dyn_client=ocp_client):
            with contextlib.suppress(NotFoundError):
                _pv_spec = _pv.instance.spec.to_dict()
                if partial_name in _pv_spec.get("claimRef", {}).get("name", ""):
                    if _pv.instance.status.phase != _pv.Status.RELEASED:
                        pvs_to_wait.append(_pv)
    except Exception as exc:
        LOGGER.error(f"Failed to get PVs: {exc}")

    if pvs_to_wait:
        LOGGER.info(f"Waiting for {len(pvs_to_wait)} PVs to be deleted in parallel...")
        with ThreadPoolExecutor(max_workers=min(len(pvs_to_wait), 10)) as executor:
            future_to_pv = {executor.submit(wait_for_resource_deletion, pv, "PV"): pv for pv in pvs_to_wait}
            for future in as_completed(future_to_pv):
                result = future.result()
                if not result["success"]:
                    leftovers = append_leftovers(leftovers=leftovers, resource=result["resource"])

    return leftovers


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
    ocp_admin_client: DynamicClient,
    plan: dict[str, Any],
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
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

    if not destination_provider.ocp_resource:
        raise ValueError("destination_provider.ocp_resource is not set")

    test_name = request._pyfuncitem.name
    _source_provider_type = py_config.get("source_provider_type")

    # Plan CR accepts only VM name/id
    virtual_machines_list: list[dict[str, str]] = [{"name": vm["name"]} for vm in plan["virtual_machines"]]

    if _source_provider_type == Provider.ProviderType.OPENSHIFT:
        for idx in range(len(virtual_machines_list)):
            virtual_machines_list[idx].update({"namespace": source_vms_namespace})

    return {
        "ocp_admin_client": ocp_admin_client,
        "source_provider_name": source_provider.ocp_resource.name,
        "source_provider_namespace": source_provider.ocp_resource.namespace,
        "virtual_machines_list": virtual_machines_list,
        "warm_migration": plan.get("warm_migration", False),
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
