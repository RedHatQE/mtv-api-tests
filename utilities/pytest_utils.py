from __future__ import annotations

import contextlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

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
    from xml.etree.ElementTree import Element

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
    for AI provider and model. Disables AI analysis if JJI_SERVER_URL is missing
    or if pytest was invoked with --collectonly or --setupplan.

    Args:
        session (pytest.Session): The pytest session object.
    """
    if is_dry_run(session.config):
        session.config.option.analyze_with_ai = False
        return

    load_dotenv()

    LOGGER.info("Setting up AI-powered test failure analysis")

    if not os.environ.get("JJI_SERVER_URL"):
        LOGGER.warning("JJI_SERVER_URL is not set. Analyze with AI features will be disabled.")
        session.config.option.analyze_with_ai = False

    else:
        if not os.environ.get("JJI_AI_PROVIDER"):
            os.environ["JJI_AI_PROVIDER"] = "claude"

        if not os.environ.get("JJI_AI_MODEL"):
            os.environ["JJI_AI_MODEL"] = "claude-opus-4-6[1m]"


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
            source_provider_data = session_store["source_provider_data"]

            with VMWareProvider(
                host=source_provider_data["fqdn"],
                username=source_provider_data["username"],
                password=source_provider_data["password"],
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
            leftovers.setdefault(Provider.ProviderType.VSPHERE, vmware_cloned_vms)

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
    """Parse failures from JUnit XML, send for AI analysis, and enrich the XML.

    Reads the JUnit XML that pytest already generated, extracts all failed
    testcases, sends them to the JJI server for AI analysis, and injects
    the analysis results back into the same XML.

    Args:
        session (pytest.Session): The pytest session containing config options.

    Raises:
        Exception: Re-raises any exception from XML modification after restoring
            the original XML from backup.
    """
    xml_path_raw = getattr(session.config.option, "xmlpath", None)
    if not xml_path_raw or not Path(xml_path_raw).exists():
        return

    xml_path = Path(xml_path_raw)

    ai_provider = os.environ.get("JJI_AI_PROVIDER")
    ai_model = os.environ.get("JJI_AI_MODEL")
    if not ai_provider or not ai_model:
        LOGGER.warning("JJI_AI_PROVIDER and JJI_AI_MODEL must be set, skipping AI analysis enrichment")
        return

    failures = _extract_failures_from_xml(xml_path=xml_path)
    if not failures:
        LOGGER.info("jenkins-job-insight: No failures found in JUnit XML, skipping AI analysis")
        return

    server_url = os.environ["JJI_SERVER_URL"]
    payload: dict[str, Any] = {
        "failures": failures,
        "ai_provider": ai_provider,
        "ai_model": ai_model,
    }

    analysis_map = _fetch_analysis_from_server(server_url=server_url, payload=payload)
    if not analysis_map:
        return

    _apply_analysis_to_xml(xml_path=xml_path, analysis_map=analysis_map)


def _extract_failures_from_xml(xml_path: Path) -> list[dict[str, str]]:
    """Extract test failures and errors from a JUnit XML file.

    Parses the XML and finds all testcase elements with failure or error
    child elements, extracting test name, error message, and stack trace.

    Args:
        xml_path (Path): Path to the JUnit XML report file.

    Returns:
        list[dict[str, str]]: List of failure dicts, each containing test_name,
            error_message, stack_trace, and status keys.
    """
    tree = ET.parse(xml_path)
    failures: list[dict[str, str]] = []

    for testcase in tree.iter("testcase"):
        failure_elem = testcase.find("failure")
        error_elem = testcase.find("error")
        result_elem = failure_elem if failure_elem is not None else error_elem

        if result_elem is None:
            continue

        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        test_name = f"{classname}.{name}" if classname else name

        failures.append({
            "test_name": test_name,
            "error_message": result_elem.get("message", ""),
            "stack_trace": result_elem.text or "",
            "status": "ERROR" if error_elem is not None and failure_elem is None else "FAILED",
        })

    return failures


def _fetch_analysis_from_server(server_url: str, payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Send collected failures to the JJI server and return the analysis map.

    Args:
        server_url (str): The JJI server base URL.
        payload (dict[str, Any]): Request payload containing failures and AI config.

    Returns:
        dict[tuple[str, str], dict[str, Any]]: Mapping of (classname, test_name) to
            analysis results. Returns empty dict on request failure.
    """
    try:
        timeout_value = int(os.environ.get("JJI_TIMEOUT", "600"))
    except ValueError:
        LOGGER.warning("Invalid JJI_TIMEOUT value, using default 600 seconds")
        timeout_value = 600

    try:
        response = requests.post(
            f"{server_url.rstrip('/')}/analyze-failures",
            json=payload,
            timeout=timeout_value,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        error_detail = ""
        if isinstance(exc, requests.RequestException) and exc.response is not None:
            try:
                error_detail = f" Response: {exc.response.text}"
            except Exception as detail_exc:
                LOGGER.debug(f"Could not extract response detail: {detail_exc}")
        LOGGER.error(f"Server request failed: {exc}{error_detail}")
        return {}

    analysis_map: dict[tuple[str, str], dict[str, Any]] = {}
    for failure in result.get("failures", []):
        test_name = failure.get("test_name", "")
        analysis = failure.get("analysis", {})
        if test_name and analysis:
            # test_name is "classname.name" from XML extraction; split on last dot
            dot_idx = test_name.rfind(".")
            if dot_idx > 0:
                analysis_map[(test_name[:dot_idx], test_name[dot_idx + 1 :])] = analysis
            else:
                analysis_map[("", test_name)] = analysis

    return analysis_map


def _apply_analysis_to_xml(xml_path: Path, analysis_map: dict[tuple[str, str], dict[str, Any]]) -> None:
    """Apply AI analysis results to JUnit XML testcase elements.

    Uses exact (classname, name) matching since failures are extracted from
    the same XML file, guaranteeing identical attribute values.
    Backs up the original XML before modification and restores it on failure.

    Args:
        xml_path (Path): Path to the JUnit XML report file.
        analysis_map (dict[tuple[str, str], dict[str, Any]]): Mapping of
            (classname, test_name) to analysis results.

    Raises:
        Exception: Re-raises any exception from XML parsing/writing after
            restoring the original XML from backup.
    """
    backup_path = xml_path.with_suffix(".xml.bak")
    shutil.copy2(xml_path, backup_path)

    try:
        tree = ET.parse(xml_path)
        matched_keys: set[tuple[str, str]] = set()
        for testcase in tree.iter("testcase"):
            key = (testcase.get("classname", ""), testcase.get("name", ""))
            analysis = analysis_map.get(key)
            if analysis:
                _inject_analysis(testcase, analysis)
                matched_keys.add(key)

        unmatched = set(analysis_map.keys()) - matched_keys
        if unmatched:
            LOGGER.warning(
                f"jenkins-job-insight: {len(unmatched)} analysis results did not match any testcase: {unmatched}"
            )

        tree.write(str(xml_path), encoding="unicode", xml_declaration=True)
        backup_path.unlink()  # Success - remove backup
    except Exception:
        # Restore original XML from backup
        shutil.copy2(backup_path, xml_path)
        backup_path.unlink()
        raise


def _inject_analysis(testcase: Element, analysis: dict[str, Any]) -> None:
    """Inject AI analysis into a JUnit XML testcase element.

    Adds structured properties (classification, code fix, bug report) and a
    human-readable summary to the testcase's system-out section.

    Args:
        testcase (Element): The XML testcase element to enrich.
        analysis (dict[str, Any]): Analysis dict with keys like classification,
            details, affected_tests, code_fix, and product_bug_report.
    """
    # Add structured properties
    properties = testcase.find("properties")
    if properties is None:
        properties = ET.SubElement(testcase, "properties")

    _add_property(properties, "ai_classification", analysis.get("classification", ""))
    _add_property(properties, "ai_details", analysis.get("details", ""))

    affected = analysis.get("affected_tests", [])
    if affected:
        _add_property(properties, "ai_affected_tests", ", ".join(affected))

    # Code fix properties
    code_fix = analysis.get("code_fix")
    if code_fix and isinstance(code_fix, dict):
        _add_property(properties, "ai_code_fix_file", code_fix.get("file", ""))
        _add_property(properties, "ai_code_fix_line", str(code_fix.get("line", "")))
        _add_property(properties, "ai_code_fix_change", code_fix.get("change", ""))

    # Product bug properties
    bug_report = analysis.get("product_bug_report")
    if bug_report and isinstance(bug_report, dict):
        _add_property(properties, "ai_bug_title", bug_report.get("title", ""))
        _add_property(properties, "ai_bug_severity", bug_report.get("severity", ""))
        _add_property(properties, "ai_bug_component", bug_report.get("component", ""))
        _add_property(properties, "ai_bug_description", bug_report.get("description", ""))

    # Add human-readable system-out
    text = _format_analysis_text(analysis)
    if text:
        system_out = testcase.find("system-out")
        if system_out is None:
            system_out = ET.SubElement(testcase, "system-out")
            system_out.text = text
        else:
            # Append to existing system-out
            existing = system_out.text or ""
            system_out.text = f"{existing}\n\n--- AI Analysis ---\n{text}" if existing else text


def _add_property(properties_elem: Element, name: str, value: str) -> None:
    """Add a property sub-element to a properties element if value is non-empty.

    Args:
        properties_elem (Element): The parent properties XML element.
        name (str): The property name attribute.
        value (str): The property value attribute. Skipped if falsy.
    """
    if value:
        prop = ET.SubElement(properties_elem, "property")
        prop.set("name", name)
        prop.set("value", value)


def _format_analysis_text(analysis: dict[str, Any]) -> str:
    """Format an analysis dict as human-readable text for JUnit system-out.

    Args:
        analysis (dict[str, Any]): Analysis dict with classification, details,
            code_fix, and product_bug_report keys.

    Returns:
        str: Formatted multi-line text summary, or empty string if no content.
    """
    parts = []

    classification = analysis.get("classification", "")
    if classification:
        parts.append(f"Classification: {classification}")

    details = analysis.get("details", "")
    if details:
        parts.append(f"\n{details}")

    code_fix = analysis.get("code_fix")
    if code_fix and isinstance(code_fix, dict):
        parts.append("\nCode Fix:")
        parts.append(f"  File: {code_fix.get('file', '')}")
        parts.append(f"  Line: {code_fix.get('line', '')}")
        parts.append(f"  Change: {code_fix.get('change', '')}")

    bug_report = analysis.get("product_bug_report")
    if bug_report and isinstance(bug_report, dict):
        parts.append("\nProduct Bug:")
        parts.append(f"  Title: {bug_report.get('title', '')}")
        parts.append(f"  Severity: {bug_report.get('severity', '')}")
        parts.append(f"  Component: {bug_report.get('component', '')}")
        parts.append(f"  Description: {bug_report.get('description', '')}")

    return "\n".join(parts) if parts else ""
