import contextlib
import json
from pathlib import Path
from typing import Any

from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.datavolume import DataVolume
from ocp_resources.host import Host
from ocp_resources.migration import Migration
from ocp_resources.namespace import Namespace
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.network_map import NetworkMap
from ocp_resources.persistent_volume import PersistentVolume
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import NamespacedResource, Resource, ResourceEditor, get_client
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine import VirtualMachine
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError

LOGGER = get_logger(__name__)


def session_teardown(session_store: dict[str, Any]) -> None:
    LOGGER.info("Running teardown to delete all created resources")

    ocp_client = get_client()
    session_teardown_resources = session_store["teardown"]
    target_namespace = session_store["target_namespace"]
    session_uuid = session_store["session_uuid"]
    leftovers: dict[str, list[dict[str, str]]] = {}

    try:
        cancel_migrations(
            migrations=session_teardown_resources.get(Migration.kind, []),
            ocp_client=ocp_client,
            target_namespace=target_namespace,
            leftovers=leftovers,
        )
        archive_plans(
            plans=session_teardown_resources.get(Plan.kind, []),
            ocp_client=ocp_client,
            target_namespace=target_namespace,
            session_uuid=session_uuid,
            leftovers=leftovers,
        )

    finally:
        teardown_resources(
            session_teardown_resources=session_teardown_resources,
            ocp_client=ocp_client,
            target_namespace=target_namespace,
            session_uuid=session_uuid,
            leftovers=leftovers,
        )


def collect_created_resources(session_store: dict[str, Any], data_collector_path: Path) -> None:
    resources = session_store["teardown"]

    if resources:
        try:
            LOGGER.info(f"Write created resources data to {data_collector_path}/resources.json")
            with open(data_collector_path / "resources.json", "w") as fd:
                json.dump(session_store["teardown"], fd)

        except Exception as ex:
            LOGGER.error(f"Failed to store resources.json due to: {ex}")


def cancel_migrations(
    migrations: list[dict[str, str]],
    ocp_client: DynamicClient,
    target_namespace: str,
    leftovers: dict[str, list[dict[str, str]]],
) -> dict[str, list[dict[str, str]]]:
    migrations_to_cancel: list[Migration] = []

    for _migration in migrations:
        migration = Migration(name=_migration["name"], namespace=_migration["namespace"], client=ocp_client)

        for condition in migration.instance.status.conditions:
            # Only cancel migrations that are in "Executing" state
            if condition.type == "Executing" and condition.status == migration.Condition.Status.TRUE:
                migrations_to_cancel.append(migration)
                break

    for migration in migrations_to_cancel:
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
            plan.wait_for_condition(condition=plan.Condition.CANCELED, status=plan.Condition.Status.TRUE)
        except TimeoutExpiredError:
            LOGGER.error(f"Failed to cancel migration {migration.name}")
            continue

        leftovers = _check_dv_pvc_pv_deleted(
            leftovers=leftovers, ocp_client=ocp_client, target_namespace=target_namespace, partial_name=migration.name
        )

    return leftovers


def archive_plans(
    plans: list[dict[str, str]],
    ocp_client: DynamicClient,
    target_namespace: str,
    session_uuid: str,
    leftovers: dict[str, list[dict[str, str]]],
) -> dict[str, list[dict[str, str]]]:
    for _plan in plans:
        plan = Plan(name=_plan["name"], namespace=_plan["namespace"], client=ocp_client)
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
        except TimeoutExpiredError:
            LOGGER.error(f"Failed to archive plan {plan.name}")
            continue

    # Make sure pods are deleted after archiving the plan.
    for _pod in Pod.get(dyn_client=ocp_client, namespace=target_namespace):
        if session_uuid in _pod.name:
            if not _pod.wait_deleted():
                _append_leftovers(leftovers=leftovers, resource=_pod)

    return leftovers


def teardown_resources(
    session_teardown_resources: dict[str, list[dict[str, str]]],
    ocp_client: DynamicClient,
    session_uuid: str,
    target_namespace: str,
    leftovers: dict[str, list[dict[str, str]]],
) -> bool:
    """
    Delete all the resources that was created by the tests.
    Check that resources that was created by the migration is deleted
    Report if we have any leftovers in the cluster and return False if any, else return True
    """
    # Resources that was created by the tests
    migrations = session_teardown_resources.get(Migration.kind, [])
    plans = session_teardown_resources.get(Plan.kind, [])
    providers = session_teardown_resources.get(Provider.kind, [])
    hosts = session_teardown_resources.get(Host.kind, [])
    secrets = session_teardown_resources.get(Secret.kind, [])
    network_attachment_definitions = session_teardown_resources.get(NetworkAttachmentDefinition.kind, [])
    networkmaps = session_teardown_resources.get(NetworkMap.kind, [])
    namespaces = session_teardown_resources.get(Namespace.kind, [])
    storagemaps = session_teardown_resources.get(StorageMap.kind, [])

    # Resources that was created by running migration
    pods = session_teardown_resources.get(Pod.kind, [])
    virtual_machines = session_teardown_resources.get(VirtualMachine.kind, [])

    # Clean all resources that was created by the tests
    for migration in migrations:
        migration_obj = Migration(name=migration["name"], namespace=migration["namespace"], client=ocp_client)
        if not migration_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=migration_obj)

    for plan in plans:
        plan_obj = Plan(name=plan["name"], namespace=plan["namespace"], client=ocp_client)
        if not plan_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=plan_obj)

    for provider in providers:
        provider_obj = Provider(name=provider["name"], namespace=provider["namespace"], client=ocp_client)
        if not provider_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=provider_obj)

    for host in hosts:
        host_obj = Host(name=host["name"], namespace=host["namespace"], client=ocp_client)
        if not host_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=host_obj)

    for secret in secrets:
        secret_obj = Secret(name=secret["name"], namespace=secret["namespace"], client=ocp_client)
        if not secret_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=secret_obj)

    for network_attachment_definition in network_attachment_definitions:
        network_attachment_definition_obj = NetworkAttachmentDefinition(
            name=network_attachment_definition["name"],
            namespace=network_attachment_definition["namespace"],
            client=ocp_client,
        )
        if not network_attachment_definition_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=network_attachment_definition_obj)

    for storagemap in storagemaps:
        storagemap_obj = StorageMap(name=storagemap["name"], namespace=storagemap["namespace"], client=ocp_client)
        if not storagemap_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=storagemap_obj)

    for networkmap in networkmaps:
        networkmap_obj = NetworkMap(name=networkmap["name"], namespace=networkmap["namespace"], client=ocp_client)
        if not networkmap_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=networkmap_obj)

    for namespace in namespaces:
        namespace_obj = Namespace(name=namespace["name"], client=ocp_client)
        if not namespace_obj.clean_up(wait=True):
            _append_leftovers(leftovers=leftovers, resource=namespace_obj)

    # Check that resources that was created by running migration are deleted
    for virtual_machine in virtual_machines:
        virtual_machine_obj = VirtualMachine(
            name=virtual_machine["name"], namespace=virtual_machine["namespace"], client=ocp_client
        )
        if virtual_machine_obj.exists:
            if not virtual_machine_obj.clean_up(wait=True):
                _append_leftovers(leftovers=leftovers, resource=virtual_machine_obj)

    for pod in pods:
        pod_obj = Pod(name=pod["name"], namespace=pod["namespace"], client=ocp_client)
        if pod_obj.exists:
            if not pod_obj.clean_up(wait=True):
                _append_leftovers(leftovers=leftovers, resource=pod_obj)

    leftovers = _check_dv_pvc_pv_deleted(
        leftovers=leftovers, ocp_client=ocp_client, target_namespace=target_namespace, partial_name=session_uuid
    )

    # Report if we have any leftovers
    if leftovers:
        LOGGER.error(f"Failed to clean up the following resources: {leftovers}")
        return False

    return True


def _check_dv_pvc_pv_deleted(
    leftovers: dict[str, list[dict[str, str]]],
    ocp_client: DynamicClient,
    target_namespace: str,
    partial_name: str,
) -> dict[str, list[dict[str, str]]]:
    # When migration in not canceled (succeeded) DVs,PVCs are deleted only when the target_namespace is deleted
    # All calls wrap with `with contextlib.suppress(NotFoundError)` since the resources can be gone even after we get it.
    for _dv in DataVolume.get(dyn_client=ocp_client, namespace=target_namespace):
        with contextlib.suppress(NotFoundError):
            if partial_name in _dv.name:
                if not _dv.wait_deleted():
                    _append_leftovers(leftovers=leftovers, resource=_dv)

    for _pvc in PersistentVolumeClaim.get(dyn_client=ocp_client, namespace=target_namespace):
        with contextlib.suppress(NotFoundError):
            if partial_name in _pvc.name:
                if not _pvc.wait_deleted():
                    _append_leftovers(leftovers=leftovers, resource=_pvc)

    for _pv in PersistentVolume.get(dyn_client=ocp_client):
        with contextlib.suppress(NotFoundError):
            _pv_spec = _pv.instance.spec.to_dict()
            if partial_name in _pv_spec.get("claimRef", {}).get("name", ""):
                if not _pv.wait_deleted():
                    _append_leftovers(leftovers=leftovers, resource=_pv)

    return leftovers


def _append_leftovers(
    leftovers: dict[str, list[dict[str, str]]], resource: Resource | NamespacedResource
) -> dict[str, list[dict[str, str]]]:
    _name = resource.name
    _namespace = resource.namespace
    _kind = resource.kind

    leftovers.setdefault(_kind, []).append({
        "name": _name,
        "namespace": _namespace,
    })

    return leftovers
