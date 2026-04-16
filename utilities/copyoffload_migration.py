"""
Copy-offload migration utilities for MTV tests.

This module provides copy-offload specific functionality for VM migration tests,
including credential management, cloud-init readiness checks, and XCOPY validation.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from ocp_resources.pod import Pod
from ocp_resources.secret import Secret
from rrmngmnt import Host, RootUser, User
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from utilities.post_migration import get_ssh_credentials_from_provider_config

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.providers.vmware import VMWareProvider
    from ocp_resources.plan import Plan

LOGGER = get_logger(__name__)


def get_copyoffload_credential(
    credential_name: str,
    copyoffload_config: dict[str, Any],
) -> str | None:
    """
    Get a copyoffload credential from environment variable or config file.

    Environment variables take precedence over config file values.
    Environment variable names are constructed as COPYOFFLOAD_{credential_name.upper()}.

    Args:
        credential_name: Name of the credential (e.g., "storage_hostname", "ontap_svm",
                        "vantara_hostgroup_id_list")
        copyoffload_config: Copyoffload configuration dictionary

    Returns:
        str | None: Credential value from env var or config, or None if not found

    Examples:
        - "storage_hostname" → "COPYOFFLOAD_STORAGE_HOSTNAME"
        - "ontap_svm" → "COPYOFFLOAD_ONTAP_SVM"
        - "vantara_hostgroup_id_list" → "COPYOFFLOAD_VANTARA_HOSTGROUP_ID_LIST"
    """
    env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
    return os.getenv(env_var_name) or copyoffload_config.get(credential_name)


def wait_for_plan_secret(ocp_admin_client: DynamicClient, namespace: str, plan_name: str) -> None:
    """
    Wait for Forklift to create plan-specific secret for copy-offload.

    When a Plan is created with copy-offload configuration, ForkliftController
    should automatically create a plan-specific secret containing storage credentials.
    This function polls for that secret's existence.

    Args:
        ocp_admin_client: OpenShift dynamic client
        namespace: Namespace where the plan and secret exist
        plan_name: Name of the Plan (secret will be named {plan_name}-*)

    Note:
        Times out after 60 seconds but continues anyway (logs warning).
        The migration will fail with clearer error if secret is missing.
    """
    LOGGER.info("Copy-offload: waiting for Forklift to create plan-specific secret...")
    try:
        for _ in TimeoutSampler(
            wait_timeout=60,
            sleep=2,
            func=lambda: any(
                s.name.startswith(f"{plan_name}-") for s in Secret.get(client=ocp_admin_client, namespace=namespace)
            ),
        ):
            break
    except TimeoutExpiredError:
        LOGGER.warning(f"Timeout waiting for plan secret '{plan_name}-*' - continuing anyway")


def wait_for_vmware_cloud_init_all_vms(
    prepared_plan: dict[str, Any],
    source_provider: VMWareProvider,
    source_provider_data: dict[str, Any],
) -> None:
    """Wait for cloud-init to finish on all VMware VMs in the plan.

    Iterates over all VMs in the plan and waits for each to signal
    cloud-init completion via the presence of ``/cloud-init.finish``.

    Args:
        prepared_plan (dict[str, Any]): Processed plan config with VM data
        source_provider (VMWareProvider): Source VMware provider instance
        source_provider_data (dict[str, Any]): Source provider configuration data

    Raises:
        TimeoutExpiredError: If cloud-init does not finish within timeout
        ValueError: If guest info or IP address is unavailable
    """
    for vm_data in prepared_plan["virtual_machines"]:
        vm_name = vm_data["name"]
        provider_vm_api = prepared_plan["source_vms_data"][vm_name]["provider_vm_api"]

        cloud_init_kwargs: dict[str, Any] = {
            "source_provider": source_provider,
            "source_provider_data": source_provider_data,
            "vm_name": vm_name,
            "provider_vm_api": provider_vm_api,
            "file_name": "/cloud-init.finish",
        }
        if "source_vm_power" in vm_data:
            cloud_init_kwargs["target_power_state"] = vm_data["source_vm_power"]

        wait_for_cloud_init(**cloud_init_kwargs)


def wait_for_cloud_init(
    source_provider: VMWareProvider,
    source_provider_data: dict[str, Any],
    vm_name: str,
    provider_vm_api: Any,
    file_name: str,
    timeout: int = 2000,
    target_power_state: str = "off",
) -> None:
    """
    Wait for cloud-init to finish by checking for a specific file.

    Args:
        source_provider: Source provider instance
        source_provider_data: Source provider configuration data
        vm_name: Name of the VM
        provider_vm_api: Provider VM object
        file_name: Full path to the file to check for (e.g., "/cloud-init.finish")
        timeout: Timeout in seconds (default: 2000)
        target_power_state: Expected source VM power state for downstream validation ("on" or "off",
            default: "off"). When "off", logs that MTV will handle shutdown. Does not change VM power.

    Raises:
        TimeoutExpiredError: If cloud-init does not finish within timeout
        ValueError: If guest info or IP address is unavailable
    """
    LOGGER.info(f"Powering on VM {vm_name} to check cloud-init status")
    source_provider.start_vm(provider_vm_api)

    try:
        # Wait for IP
        if not source_provider.wait_for_vmware_guest_info(provider_vm_api, timeout=1000):
            raise ValueError(f"Guest info not available for VM '{vm_name}'")

        # Get IP with polling
        ip_address = None
        last_vm_info: dict[str, Any] = {}

        def _get_ip() -> str | None:
            nonlocal last_vm_info
            last_vm_info = source_provider.vm_dict(provider_vm_api=provider_vm_api)
            for nic in last_vm_info.get("network_interfaces", []):
                if nic.get("ip_addresses"):
                    return nic["ip_addresses"][0]["ip_address"]
            return None

        try:
            for ip in TimeoutSampler(wait_timeout=300, sleep=5, func=_get_ip):
                if ip:
                    ip_address = ip
                    break
        except TimeoutExpiredError:
            pass

        if not ip_address:
            raise ValueError(f"Could not find IP address for VM '{vm_name}'")

        LOGGER.info(f"VM {vm_name} has IP: {ip_address}")

        # Get credentials
        source_vm_info = {"win_os": last_vm_info.get("win_os", False)}
        username, password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_info)

        host = Host(ip_address)
        user = RootUser(password) if username == "root" else User(username, password)

        def _check_file() -> bool:
            try:
                rc, _, _ = host.executor(user=user).run_cmd(["ls", file_name])
                return rc == 0
            except Exception as e:
                LOGGER.warning(f"SSH check failed for {vm_name}: {type(e).__name__}: {e} - retrying...")
                return False

        LOGGER.info(f"Waiting for {file_name} on {ip_address}...")
        try:
            for sample in TimeoutSampler(wait_timeout=timeout, sleep=10, func=_check_file):
                if sample:
                    LOGGER.info(f"{file_name} found!")
                    break
        except TimeoutExpiredError:
            raise TimeoutExpiredError(f"Cloud-init did not finish (file {file_name} not found)") from None

    finally:
        if target_power_state == "off":
            LOGGER.info(f"VM {vm_name} left powered on — MTV will handle shutdown for cold migration")
        else:
            LOGGER.info(f"Leaving VM {vm_name} powered on")


def _get_migration_uid(plan: Plan) -> str:
    """Extract the migration UID from a completed Plan's status.

    Args:
        plan (Plan): The Plan CR resource.

    Returns:
        str: The migration UID from the first history entry.

    Raises:
        ValueError: If plan status, migration, history, or UID is missing.
    """
    plan_status = plan.instance.status
    if plan_status is None:
        raise ValueError(f"Plan '{plan.name}' has no status")

    migration = plan_status.migration
    if migration is None:
        raise ValueError(f"Plan '{plan.name}' has no migration in status")

    migration_history = migration.history
    if not migration_history:
        raise ValueError(f"Plan '{plan.name}' has no migration history")

    first_history = migration_history[0]
    migration_ref = first_history.migration
    if not migration_ref or not migration_ref.uid:
        raise ValueError(f"Plan '{plan.name}' migration history has no migration UID")

    return migration_ref.uid


def _find_populate_pods(ocp_admin_client: DynamicClient, namespace: str, migration_uid: str) -> list[Pod]:
    """Find populate pods for a given migration.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where populate pods exist.
        migration_uid (str): Migration UID to filter pods by.

    Returns:
        list[Pod]: List of populate pods.

    Raises:
        ValueError: If no populate pods are found.
    """
    populate_pods: list[Pod] = [
        pod
        for pod in Pod.get(
            client=ocp_admin_client,
            namespace=namespace,
            label_selector=f"migration={migration_uid}",
        )
        if pod.name.startswith("populate-")
    ]

    if not populate_pods:
        raise ValueError(f"No populate pods found for migration '{migration_uid}' in namespace '{namespace}'")

    return populate_pods


def _parse_xcopy_used_from_log(pod: Pod) -> int:
    """Parse the last xcopyUsed value from a populate pod's logs.

    Args:
        pod (Pod): The populate pod to read logs from.

    Returns:
        int: The last xcopyUsed value (0 or 1).

    Raises:
        ValueError: If xcopyUsed is not found in the pod logs.
    """
    log_content: str = pod.log()
    matches: list[str] = re.findall(r"xcopyUsed=(\d+)", log_content)
    if not matches:
        raise ValueError(f"xcopyUsed not found in populate pod '{pod.name}' logs")

    return int(matches[-1])


def verify_xcopy_used(
    ocp_admin_client: DynamicClient,
    plan: Plan,
    target_namespace: str,
    expected_xcopy_used: bool,
) -> None:
    """Verify xcopyUsed matches expected value for all disks in a copy-offload migration.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
        plan (Plan): The Plan CR resource (used to find the migration UID).
        target_namespace (str): Namespace where populate pods exist.
        expected_xcopy_used (bool): Expected xcopyUsed value.
            True (xcopyUsed=1) for XCOPY-capable datastores.
            False (xcopyUsed=0) for fallback/non-XCOPY datastores.

    Raises:
        ValueError: If no populate pods found or xcopyUsed not found in pod logs.
        AssertionError: If any disk's xcopyUsed value doesn't match expected.
    """
    migration_uid: str = _get_migration_uid(plan=plan)
    LOGGER.info(f"Checking xcopyUsed for migration '{migration_uid}'")

    populate_pods: list[Pod] = _find_populate_pods(
        ocp_admin_client=ocp_admin_client,
        namespace=target_namespace,
        migration_uid=migration_uid,
    )
    LOGGER.info(f"Found {len(populate_pods)} populate pod(s)")

    expected_value: int = 1 if expected_xcopy_used else 0

    for pod in populate_pods:
        pvc_name: str = pod.instance.metadata.labels.get("pvcName", pod.name)
        xcopy_used: int = _parse_xcopy_used_from_log(pod=pod)
        LOGGER.info(f"Pod '{pod.name}' (PVC '{pvc_name}'): xcopyUsed={xcopy_used}")

        assert xcopy_used == expected_value, (
            f"Pod '{pod.name}' (PVC '{pvc_name}'): expected xcopyUsed={expected_value}, got xcopyUsed={xcopy_used}"
        )
