from __future__ import annotations

import contextlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from dotenv import load_dotenv
from ocp_resources.host import Host
from ocp_resources.migration import Migration
from ocp_resources.namespace import Namespace
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine import VirtualMachine
from simple_logger.logger import get_logger

from exceptions.exceptions import SessionTeardownError
from libs.providers.openstack import OpenStackProvider
from libs.providers.rhv import OvirtProvider
from libs.providers.vmware import VMWareProvider
from utilities.migration_utils import append_leftovers, archive_plan, cancel_migration, check_dv_pvc_pv_deleted
from utilities.utils import delete_all_vms, get_cluster_client

if TYPE_CHECKING:
    import pytest
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)


def is_dry_run(config: pytest.Config) -> bool:
    """Check if pytest was invoked in dry-run mode (collectonly or setupplan).

    Args:
        config (pytest.Config): The pytest config object.

    Returns:
        bool: True if pytest is in collectonly or setupplan mode.
    """
    return config.option.setupplan or config.option.collectonly


def prepare_base_path(base_path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        # When running pytest in parallel (-n) we may get here error even when path exists
        if base_path.exists():
            shutil.rmtree(base_path)

    base_path.mkdir(parents=True, exist_ok=True)


def setup_ai_analysis(session: pytest.Session) -> None:
    """Configure AI analysis for test failure reporting.

    Loads environment variables, validates prerequisites, and sets defaults
    for AI provider and model. Disables AI analysis if ROOTCOZ_SERVER_URL is missing
    or if pytest was invoked with --collectonly or --setupplan.

    Args:
        session (pytest.Session): The pytest session object.
    """
    if is_dry_run(session.config):
        session.config.option.analyze_with_ai = False
        return

    load_dotenv()

    LOGGER.info("Setting up AI-powered test failure analysis")

    if not os.environ.get("ROOTCOZ_SERVER_URL"):
        LOGGER.warning("ROOTCOZ_SERVER_URL is not set. Analyze with AI features will be disabled.")
        session.config.option.analyze_with_ai = False

    else:
        if not os.environ.get("ROOTCOZ_AI_PROVIDER"):
            os.environ["ROOTCOZ_AI_PROVIDER"] = "claude"

        if not os.environ.get("ROOTCOZ_AI_MODEL"):
            os.environ["ROOTCOZ_AI_MODEL"] = "claude-opus-4-6[1m]"


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

    ocp_client = get_cluster_client()

    # When running in parallel (-n auto) `session_store` can be empty.
    if session_teardown_resources := session_store.get("teardown"):
        for migration_name in session_teardown_resources.get(Migration.kind, []):
            migration = Migration(name=migration_name["name"], namespace=migration_name["namespace"], client=ocp_client)
            cancel_migration(migration=migration)

        for plan_name in session_teardown_resources.get(Plan.kind, []):
            plan = Plan(name=plan_name["name"], namespace=plan_name["namespace"], client=ocp_client)
            archive_plan(plan=plan)

        leftovers = teardown_resources(
            session_store=session_store,
            ocp_client=ocp_client,
            target_namespace=session_store.get("target_namespace"),
        )
        if leftovers:
            raise SessionTeardownError(f"Failed to clean up the following resources: {leftovers}")


def teardown_resources(
    session_store: dict[str, Any],
    ocp_client: DynamicClient,
    target_namespace: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """
    Delete all the resources that was created by the tests.
    Check that resources that was created by the migration is deleted
    Report if we have any leftovers in the cluster and return False if any, else return True
    """
    leftovers: dict[str, list[dict[str, str]]] = {}
    session_teardown_resources: dict[str, list[dict[str, str]]] = session_store["teardown"]
    session_uuid = session_store["session_uuid"]

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
    vmware_cloned_vms = session_teardown_resources.get(Provider.ProviderType.VSPHERE, [])
    openstack_cloned_vms = session_teardown_resources.get(Provider.ProviderType.OPENSTACK, [])
    rhv_cloned_vms = session_teardown_resources.get(Provider.ProviderType.RHV, [])
    openstack_volume_snapshots = session_teardown_resources.get("VolumeSnapshot", [])

    # Resources that was created by running migration
    pods = session_teardown_resources.get(Pod.kind, [])
    virtual_machines = session_teardown_resources.get(VirtualMachine.kind, [])

    # Clean all resources that was created by the tests
    for migration in migrations:
        try:
            migration_obj = Migration(name=migration["name"], namespace=migration["namespace"], client=ocp_client)
            if not migration_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=migration_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup migration {migration['name']}: {exc}")
            leftovers.setdefault(Migration.kind, []).append(migration)

    for plan in plans:
        try:
            plan_obj = Plan(name=plan["name"], namespace=plan["namespace"], client=ocp_client)
            if not plan_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=plan_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup plan {plan['name']}: {exc}")
            leftovers.setdefault(Plan.kind, []).append(plan)

    for provider in providers:
        try:
            provider_obj = Provider(name=provider["name"], namespace=provider["namespace"], client=ocp_client)
            if not provider_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=provider_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup provider {provider['name']}: {exc}")
            leftovers.setdefault(Provider.kind, []).append(provider)

    for host in hosts:
        try:
            host_obj = Host(name=host["name"], namespace=host["namespace"], client=ocp_client)
            if not host_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=host_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup host {host['name']}: {exc}")
            leftovers.setdefault(Host.kind, []).append(host)

    for secret in secrets:
        try:
            secret_obj = Secret(name=secret["name"], namespace=secret["namespace"], client=ocp_client)
            if not secret_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=secret_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup secret {secret['name']}: {exc}")
            leftovers.setdefault(Secret.kind, []).append(secret)

    for network_attachment_definition in network_attachment_definitions:
        try:
            network_attachment_definition_obj = NetworkAttachmentDefinition(
                name=network_attachment_definition["name"],
                namespace=network_attachment_definition["namespace"],
                client=ocp_client,
            )
            if not network_attachment_definition_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=network_attachment_definition_obj)
        except Exception as exc:
            LOGGER.error(
                f"Failed to cleanup NetworkAttachmentDefinition {network_attachment_definition['name']}: {exc}"
            )
            leftovers.setdefault(NetworkAttachmentDefinition.kind, []).append(network_attachment_definition)

    for storagemap in storagemaps:
        try:
            storagemap_obj = StorageMap(name=storagemap["name"], namespace=storagemap["namespace"], client=ocp_client)
            if not storagemap_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=storagemap_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup StorageMap {storagemap['name']}: {exc}")
            leftovers.setdefault(StorageMap.kind, []).append(storagemap)

    for networkmap in networkmaps:
        try:
            networkmap_obj = NetworkMap(name=networkmap["name"], namespace=networkmap["namespace"], client=ocp_client)
            if not networkmap_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=networkmap_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup NetworkMap {networkmap['name']}: {exc}")
            leftovers.setdefault(NetworkMap.kind, []).append(networkmap)

    # Check that resources that was created by running migration are deleted
    for virtual_machine in virtual_machines:
        try:
            virtual_machine_obj = VirtualMachine(
                name=virtual_machine["name"], namespace=virtual_machine["namespace"], client=ocp_client
            )
            if virtual_machine_obj.exists:
                if not virtual_machine_obj.clean_up(wait=True):
                    leftovers = append_leftovers(leftovers=leftovers, resource=virtual_machine_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup VirtualMachine {virtual_machine['name']}: {exc}")
            leftovers.setdefault(VirtualMachine.kind, []).append(virtual_machine)

    for pod in pods:
        try:
            pod_obj = Pod(name=pod["name"], namespace=pod["namespace"], client=ocp_client)
            if pod_obj.exists:
                if not pod_obj.clean_up(wait=True):
                    leftovers = append_leftovers(leftovers=leftovers, resource=pod_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup Pod {pod['name']}: {exc}")
            leftovers.setdefault(Pod.kind, []).append(pod)

    if target_namespace:
        try:
            delete_all_vms(ocp_admin_client=ocp_client, namespace=target_namespace)
        except Exception as exc:
            LOGGER.error(f"Failed to delete all VMs in namespace {target_namespace}: {exc}")

        # Make sure all pods related to the test session are deleted (in parallel)
        try:
            pods_to_wait = [
                _pod for _pod in Pod.get(client=ocp_client, namespace=target_namespace) if session_uuid in _pod.name
            ]

            if pods_to_wait:
                LOGGER.info(f"Waiting for {len(pods_to_wait)} pods to be deleted in parallel...")

                def wait_for_pod_deletion(pod):
                    """Helper function to wait for a single pod deletion."""
                    try:
                        if not pod.wait_deleted():
                            return {"success": False, "pod": pod, "error": None}
                        return {"success": True, "pod": pod, "error": None}
                    except Exception as exc:
                        LOGGER.error(f"Failed to wait for pod {pod.name} deletion: {exc}")
                        return {"success": False, "pod": pod, "error": exc}

                # Wait for all pods in parallel
                with ThreadPoolExecutor(max_workers=min(len(pods_to_wait), 10)) as executor:
                    future_to_pod = {executor.submit(wait_for_pod_deletion, pod): pod for pod in pods_to_wait}

                    for future in as_completed(future_to_pod):
                        result = future.result()
                        if not result["success"]:
                            leftovers = append_leftovers(leftovers=leftovers, resource=result["pod"])
        except Exception as exc:
            LOGGER.error(f"Failed to get pods in namespace {target_namespace}: {exc}")

        try:
            leftovers = check_dv_pvc_pv_deleted(
                leftovers=leftovers, ocp_client=ocp_client, target_namespace=target_namespace, partial_name=session_uuid
            )
        except Exception as exc:
            LOGGER.error(f"Failed to check DV/PVC/PV deletion: {exc}")

    if leftovers:
        LOGGER.error(
            f"There are some leftovers after tests are done, delete tests namespaces may fail. Leftovers: {leftovers}"
        )

    for namespace in namespaces:
        try:
            namespace_obj = Namespace(name=namespace["name"], client=ocp_client)
            if not namespace_obj.clean_up(wait=True):
                leftovers = append_leftovers(leftovers=leftovers, resource=namespace_obj)
        except Exception as exc:
            LOGGER.error(f"Failed to cleanup namespace {namespace['name']}: {exc}")
            leftovers.setdefault(Namespace.kind, []).append(namespace)

    if vmware_cloned_vms:
        try:
            # Use clone provider (vCenter) for cleanup when configured, to avoid
            # stale vCenter inventory records when source provider is ESXi
            cleanup_provider_data = session_store.get("clone_provider_data")
            if cleanup_provider_data is None:
                cleanup_provider_data = session_store["source_provider_data"]
                LOGGER.info(f"Using source provider '{cleanup_provider_data['fqdn']}' for VMware clone cleanup")
            else:
                LOGGER.info(
                    f"Using clone provider (vCenter) '{cleanup_provider_data['fqdn']}' for VMware clone cleanup"
                )

            with VMWareProvider(
                host=cleanup_provider_data["fqdn"],
                username=cleanup_provider_data["username"],
                password=cleanup_provider_data["password"],
            ) as vmware_provider:
                for _vm in vmware_cloned_vms:
                    _cloned_vm_name = _vm["name"]
                    try:
                        vmware_provider.delete_vm(vm_name=_cloned_vm_name)
                    except Exception as exc:
                        LOGGER.error(f"Failed to delete cloned vm {_cloned_vm_name}: {exc}")
                        leftovers.setdefault(vmware_provider.type, []).append({
                            "cloned_vm_name": _cloned_vm_name,
                        })

        except Exception as exc:
            LOGGER.error(f"Failed to connect to VMware provider for cleanup: {exc}")
            leftovers.setdefault(Provider.ProviderType.VSPHERE, []).extend(vmware_cloned_vms)

    if openstack_cloned_vms:
        try:
            source_provider_data = session_store["source_provider_data"]

            with OpenStackProvider(
                host=source_provider_data["fqdn"],
                username=source_provider_data["username"],
                password=source_provider_data["password"],
                auth_url=source_provider_data["api_url"],
                project_name=source_provider_data["project_name"],
                user_domain_name=source_provider_data["user_domain_name"],
                region_name=source_provider_data["region_name"],
                user_domain_id=source_provider_data["user_domain_id"],
                project_domain_id=source_provider_data["project_domain_id"],
            ) as openstack_provider:
                for _vm in openstack_cloned_vms:
                    _cloned_vm_name = _vm["name"]
                    try:
                        openstack_provider.delete_vm(vm_name=_cloned_vm_name)
                    except Exception as exc:
                        LOGGER.error(f"Failed to delete cloned vm {_cloned_vm_name}: {exc}")
                        leftovers.setdefault(openstack_provider.type, []).append({
                            "cloned_vm_name": _cloned_vm_name,
                        })
        except Exception as exc:
            LOGGER.error(f"Failed to connect to OpenStack provider for cleanup: {exc}")
            leftovers.setdefault(Provider.ProviderType.OPENSTACK, openstack_cloned_vms)

    if rhv_cloned_vms:
        try:
            source_provider_data = session_store["source_provider_data"]

            with OvirtProvider(
                host=source_provider_data["api_url"],
                username=source_provider_data["username"],
                password=source_provider_data["password"],
                insecure=source_provider_data.get("insecure", True),
            ) as rhv_provider:
                for _vm in rhv_cloned_vms:
                    _cloned_vm_name = _vm["name"]
                    try:
                        rhv_provider.delete_vm(vm_name=_cloned_vm_name)
                    except Exception as exc:
                        LOGGER.error(f"Failed to delete cloned vm {_cloned_vm_name}: {exc}")
                        leftovers.setdefault(rhv_provider.type, []).append({
                            "cloned_vm_name": _cloned_vm_name,
                        })
        except Exception as exc:
            LOGGER.error(f"Failed to connect to RHV provider for cleanup: {exc}")
            leftovers.setdefault(Provider.ProviderType.RHV, rhv_cloned_vms)

    if openstack_volume_snapshots:
        try:
            source_provider_data = session_store["source_provider_data"]

            with OpenStackProvider(
                host=source_provider_data["fqdn"],
                username=source_provider_data["username"],
                password=source_provider_data["password"],
                auth_url=source_provider_data["api_url"],
                project_name=source_provider_data["project_name"],
                user_domain_name=source_provider_data["user_domain_name"],
                region_name=source_provider_data["region_name"],
                user_domain_id=source_provider_data["user_domain_id"],
                project_domain_id=source_provider_data["project_domain_id"],
            ) as openstack_provider:
                for snapshot in openstack_volume_snapshots:
                    snapshot_id = snapshot["id"]
                    snapshot_name = snapshot["name"]
                    LOGGER.info(f"Deleting volume snapshot '{snapshot_name}' (ID: {snapshot_id})...")
                    openstack_provider.api.block_storage.delete_snapshot(snapshot_id, ignore_missing=True)
        except Exception as exc:
            LOGGER.error(f"Failed to connect to OpenStack provider for volume snapshot cleanup: {exc}")
            leftovers.setdefault("VolumeSnapshot", openstack_volume_snapshots)

    return leftovers


def enrich_junit_xml(session: pytest.Session) -> None:
    """Read JUnit XML, send to server for analysis, write enriched XML back.

    Reads the JUnit XML that pytest generated, POSTs the raw content to the
    rootcoz server's /analyze-failures endpoint, and writes the enriched XML
    (with analysis results) back to the same file.

    Args:
        session: The pytest session containing config options.
    """
    xml_path_raw = getattr(session.config.option, "xmlpath", None)
    if not xml_path_raw:
        LOGGER.warning("xunit file not found; pass --junitxml. Skipping AI analysis enrichment")
        return

    xml_path = Path(xml_path_raw)
    if not xml_path.exists():
        LOGGER.warning(
            "xunit file not found under %s. Skipping AI analysis enrichment",
            xml_path_raw,
        )
        return

    ai_provider = os.environ.get("ROOTCOZ_AI_PROVIDER")
    ai_model = os.environ.get("ROOTCOZ_AI_MODEL")
    if not ai_provider or not ai_model:
        LOGGER.warning("ROOTCOZ_AI_PROVIDER and ROOTCOZ_AI_MODEL must be set, skipping AI analysis enrichment")
        return

    server_url = os.environ["ROOTCOZ_SERVER_URL"]
    raw_xml = xml_path.read_text()

    try:
        timeout_value = int(os.environ.get("ROOTCOZ_TIMEOUT", "600"))
    except ValueError:
        LOGGER.warning("Invalid ROOTCOZ_TIMEOUT value, using default 600 seconds")
        timeout_value = 600

    try:
        response = requests.post(
            f"{server_url.rstrip('/')}/analyze-failures",
            json={
                "raw_xml": raw_xml,
                "ai_provider": ai_provider,
                "ai_model": ai_model,
            },
            timeout=timeout_value,
        )
        response.raise_for_status()
        result = response.json()
    except Exception as ex:
        LOGGER.exception(f"Failed to enrich JUnit XML, original preserved. {ex}")
        return

    if enriched_xml := result.get("enriched_xml"):
        xml_path.write_text(enriched_xml)
        LOGGER.info("JUnit XML enriched with AI analysis: %s", xml_path)
    else:
        LOGGER.info("No enriched XML returned (no failures or analysis failed)")
