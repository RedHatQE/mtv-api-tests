import contextlib
import json
import shutil
from pathlib import Path
from typing import Any

from kubernetes.dynamic import DynamicClient
from ocp_resources.host import Host
from ocp_resources.migration import Migration
from ocp_resources.namespace import Namespace
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.resource import get_client
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine import VirtualMachine
from simple_logger.logger import get_logger

from utilities.migration_utils import append_leftovers, archive_plan, cancel_migration, check_dv_pvc_pv_deleted

LOGGER = get_logger(__name__)


class SessionTeardownError(Exception):
    pass


def prepare_base_path(base_path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        # When running pytest in parallel (-n) we may get here error even when path exists
        if base_path.exists():
            shutil.rmtree(base_path)

    base_path.mkdir(parents=True, exist_ok=True)


def collect_created_resources(session_store: dict[str, Any], data_collector_path: Path) -> None:
    """
    collect created resources and store them in resource.json file under data collector path
    """
    resources = session_store["teardown"]

    if resources:
        try:
            LOGGER.info(f"Write created resources data to {data_collector_path}/resources.json")
            with open(data_collector_path / "resources.json", "w") as fd:
                json.dump(session_store["teardown"], fd)

        except Exception as ex:
            LOGGER.error(f"Failed to store resources.json due to: {ex}")


def session_teardown(session_store: dict[str, Any]) -> None:
    LOGGER.info("Running teardown to delete all created resources")

    ocp_client = get_client()

    # When running in parallel (-n auto) `session_store` can be empty.
    if session_teardown_resources := session_store.get("teardown"):
        for migration_name in session_teardown_resources.get(Migration.kind, []):
            migration = Migration(name=migration_name["name"], namespace=migration_name["namespace"], client=ocp_client)
            cancel_migration(migration=migration)

        for plan_name in session_teardown_resources.get(Plan.kind, []):
            plan = Plan(name=plan_name["name"], namespace=plan_name["namespace"], client=ocp_client)
            archive_plan(plan=plan)

        leftovers = teardown_resources(
            session_teardown_resources=session_teardown_resources,
            ocp_client=ocp_client,
            target_namespace=session_store.get("target_namespace"),
            session_uuid=session_store["session_uuid"],
        )
        if leftovers:
            raise SessionTeardownError(f"Failed to clean up the following resources: {leftovers}")


def teardown_resources(
    session_teardown_resources: dict[str, list[dict[str, str]]],
    ocp_client: DynamicClient,
    session_uuid: str,
    target_namespace: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """
    Delete all the resources that was created by the tests.
    Check that resources that was created by the migration is deleted
    Report if we have any leftovers in the cluster and return False if any, else return True
    """
    leftovers: dict[str, list[dict[str, str]]] = {}

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
            leftovers = append_leftovers(leftovers=leftovers, resource=migration_obj)

    for plan in plans:
        plan_obj = Plan(name=plan["name"], namespace=plan["namespace"], client=ocp_client)
        if not plan_obj.clean_up(wait=True):
            leftovers = append_leftovers(leftovers=leftovers, resource=plan_obj)

    for provider in providers:
        provider_obj = Provider(name=provider["name"], namespace=provider["namespace"], client=ocp_client)
        if not provider_obj.clean_up(wait=True):
            leftovers = append_leftovers(leftovers=leftovers, resource=provider_obj)

    for host in hosts:
        host_obj = Host(name=host["name"], namespace=host["namespace"], client=ocp_client)
        if not host_obj.clean_up(wait=True):
            leftovers = append_leftovers(leftovers=leftovers, resource=host_obj)

    for secret in secrets:
        secret_obj = Secret(name=secret["name"], namespace=secret["namespace"], client=ocp_client)
        if not secret_obj.clean_up(wait=True):
            leftovers = append_leftovers(leftovers=leftovers, resource=secret_obj)

    for network_attachment_definition in network_attachment_definitions:
        network_attachment_definition_obj = NetworkAttachmentDefinition(
            name=network_attachment_definition["name"],
            namespace=network_attachment_definition["namespace"],
            client=ocp_client,
        )
        if not network_attachment_definition_obj.clean_up(wait=True):
            leftovers = append_leftovers(leftovers=leftovers, resource=network_attachment_definition_obj)

    for storagemap in storagemaps:
        storagemap_obj = StorageMap(name=storagemap["name"], namespace=storagemap["namespace"], client=ocp_client)
        if not storagemap_obj.clean_up(wait=True):
            leftovers = append_leftovers(leftovers=leftovers, resource=storagemap_obj)

    for networkmap in networkmaps:
        networkmap_obj = NetworkMap(name=networkmap["name"], namespace=networkmap["namespace"], client=ocp_client)
        if not networkmap_obj.clean_up(wait=True):
            leftovers = append_leftovers(leftovers=leftovers, resource=networkmap_obj)

    for namespace in namespaces:
        namespace_obj = Namespace(name=namespace["name"], client=ocp_client)
        if not namespace_obj.clean_up(wait=True):
            leftovers = append_leftovers(leftovers=leftovers, resource=namespace_obj)

    # Check that resources that was created by running migration are deleted
    for virtual_machine in virtual_machines:
        virtual_machine_obj = VirtualMachine(
            name=virtual_machine["name"], namespace=virtual_machine["namespace"], client=ocp_client
        )
        if virtual_machine_obj.exists:
            if not virtual_machine_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=virtual_machine_obj)

    for pod in pods:
        pod_obj = Pod(name=pod["name"], namespace=pod["namespace"], client=ocp_client)
        if pod_obj.exists:
            if not pod_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=pod_obj)

    if target_namespace:
        # Make sure all pods related to the test session are deleted
        for _pod in Pod.get(dyn_client=ocp_client, namespace=target_namespace):
            if session_uuid in _pod.name:
                if not _pod.wait_deleted():
                    leftovers = append_leftovers(leftovers=leftovers, resource=_pod)

        leftovers = check_dv_pvc_pv_deleted(
            leftovers=leftovers, ocp_client=ocp_client, target_namespace=target_namespace, partial_name=session_uuid
        )

    return leftovers
