"""
Copy-offload migration utilities for MTV tests.

This module provides copy-offload specific functionality for VM migration tests,
including credential management, cloud-init readiness checks, and XCOPY validation.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ocp_resources.event import Event
from ocp_resources.migration import Migration
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.pod import Pod
from ocp_resources.plan import Plan
from rrmngmnt import Host, RootUser, User
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import MigrationNotFoundError
from utilities.copyoffload_constants import (
    POPULATOR_INFLIGHT_LIMIT,
    POPULATOR_THROTTLED_EVENT_REASON,
    PVC_NAME_LABEL,
    SOURCE_HOST_LABEL,
)
from utilities.copyoffload_plan_secret import wait_for_copyoffload_plan_secret
from utilities.mtv_migration import get_migration_for_plan, wait_for_migration_complate
from utilities.post_migration import get_ssh_credentials_from_provider_config
from utilities.resources import create_and_store_resource

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.providers.vmware import VMWareProvider

LOGGER = get_logger(__name__)

STORAGE_SECRET_EXTRA_ENV = "COPYOFFLOAD_STORAGE_SECRET_EXTRA"  # pragma: allowlist secret
_ACTIVE_POPULATOR_POD_PHASES = frozenset({"Running", "Pending"})


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


def _storage_secret_extra_value_as_string(value: Any) -> str:
    """Convert a config/env value to Kubernetes Secret stringData text.

    JSON booleans must become lowercase ``true``/``false``, not Python ``True``/``False``.

    Args:
        value: Raw value from JSON config or environment.

    Returns:
        str: Normalized Secret ``stringData`` text.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _secret_extra_entries_from_mapping(mapping: dict[Any, Any]) -> dict[str, str]:
    """Normalize a mapping to non-empty Secret stringData key/value pairs.

    Args:
        mapping: Raw key/value mapping from JSON config or environment.

    Returns:
        dict[str, str]: Normalized Secret keys and values.

    Raises:
        ValueError: If any key is empty after stripping.
    """
    result: dict[str, str] = {}
    for secret_key, value in mapping.items():
        if value is None:
            continue
        text = _storage_secret_extra_value_as_string(value)
        if not text:
            continue
        key = str(secret_key).strip()
        if not key:
            raise ValueError("storage_secret_extra keys must be non-empty strings")
        result[key] = text
    return result


def parse_storage_secret_extra_env() -> dict[str, str]:
    """Parse COPYOFFLOAD_STORAGE_SECRET_EXTRA as a JSON object of secret key/value pairs.

    Returns:
        dict[str, str]: Secret keys and values from the environment variable.

    Raises:
        ValueError: If the variable is set but not valid JSON object, or keys are empty.
        TypeError: If the parsed JSON value is not an object.
    """
    raw = os.getenv(STORAGE_SECRET_EXTRA_ENV, "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{STORAGE_SECRET_EXTRA_ENV} must be a valid JSON object") from exc

    if not isinstance(parsed, dict):
        raise TypeError(f"{STORAGE_SECRET_EXTRA_ENV} must be a JSON object")

    return _secret_extra_entries_from_mapping(parsed)


def get_storage_secret_extra(copyoffload_config: dict[str, Any]) -> dict[str, str]:
    """Resolve extra storage secret entries from providers JSON and environment.

    Values from ``storage_secret_extra`` in ``.providers.json`` are applied first.
    ``COPYOFFLOAD_STORAGE_SECRET_EXTRA`` (JSON object) overrides matching keys.

    Args:
        copyoffload_config: The provider ``copyoffload`` configuration dictionary.

    Returns:
        dict[str, str]: Kubernetes Secret ``stringData`` keys and values to merge.

    Raises:
        ValueError: If ``storage_secret_extra`` contains empty keys or invalid env JSON.
        TypeError: If ``storage_secret_extra`` is not a JSON object mapping.
    """
    extra: dict[str, str] = {}
    if "storage_secret_extra" not in copyoffload_config:
        extra.update(parse_storage_secret_extra_env())
        return extra

    config_extra = copyoffload_config["storage_secret_extra"]
    if config_extra is None:
        extra.update(parse_storage_secret_extra_env())
        return extra
    if not isinstance(config_extra, dict):
        raise TypeError("storage_secret_extra must be a JSON object mapping Secret keys to values")

    extra.update(_secret_extra_entries_from_mapping(config_extra))
    extra.update(parse_storage_secret_extra_env())
    return extra


def merge_storage_secret_extra(
    secret_data: dict[str, str],
    copyoffload_config: dict[str, Any],
) -> dict[str, str]:
    """Merge ``storage_secret_extra`` entries into copy-offload storage secret data.

    Extra entries override existing keys (for example vendor-mapped fields) when the
    same Secret key is specified.

    Args:
        secret_data: Base and vendor-specific secret ``stringData`` built so far.
        copyoffload_config: The provider ``copyoffload`` configuration dictionary.

    Returns:
        dict[str, str]: Updated secret data including extra entries.

    Raises:
        ValueError: If extra entries from config or environment are invalid.
        TypeError: If ``storage_secret_extra`` is not a JSON object mapping.
    """
    extra = get_storage_secret_extra(copyoffload_config)
    if not extra:
        return secret_data

    merged = dict(secret_data)
    for secret_key, value in extra.items():
        merged[secret_key] = value
        LOGGER.info(f"✓ Added extra secret field from storage_secret_extra: {secret_key}")
    return merged


def wait_for_vmware_cloud_init_all_vms(
    prepared_plan: dict[str, Any],
    source_provider: VMWareProvider,
    source_provider_data: dict[str, Any],
) -> None:
    """Wait for cloud-init to finish on all VMware VMs in the plan.

    Iterates over all VMs in the plan and waits for each to signal
    cloud-init completion via the presence of ``/var/lib/cloud/instance/boot-finished``.

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
            "file_name": "/var/lib/cloud/instance/boot-finished",
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
        file_name: Full path to the file to check for (e.g., "/var/lib/cloud/instance/boot-finished")
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


def _resolve_migration_uid(plan: Plan) -> str | None:
    """Resolve migration UID from the Migration CR.

    Returns None when the Migration CR is not created yet (e.g. during early migration polling).

    Args:
        plan (Plan): The Plan CR resource.

    Returns:
        str | None: Migration UID when the Migration CR exists, otherwise None.

    Raises:
        ValueError: If the Migration CR exists but has no UID.
    """
    try:
        migration = get_migration_for_plan(plan=plan)
    except MigrationNotFoundError:
        return None

    migration_uid = migration.instance.metadata.uid
    if not migration_uid:
        raise ValueError(f"Migration CR for Plan '{plan.name}' has no UID")
    return migration_uid


def _get_populate_pods_for_plan(
    ocp_admin_client: DynamicClient,
    plan: Plan,
    target_namespace: str,
    *,
    require_pods: bool = True,
) -> tuple[str, list[Pod]]:
    """Resolve migration UID and fetch populate pods for a completed migration.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        plan (Plan): The Plan CR resource.
        target_namespace (str): Namespace where populate pods exist.
        require_pods (bool): When True, raise if no populate pods are found.

    Returns:
        tuple[str, list[Pod]]: Migration UID and matching populate pods.

    Raises:
        ValueError: If migration UID or populate pods cannot be resolved.
    """
    migration_uid = _get_migration_uid(plan=plan)
    populate_pods = _find_populate_pods(
        ocp_admin_client=ocp_admin_client,
        namespace=target_namespace,
        migration_uid=migration_uid,
        require_pods=require_pods,
    )
    return migration_uid, populate_pods


def _find_populate_pods(
    ocp_admin_client: DynamicClient,
    namespace: str,
    migration_uid: str,
    *,
    require_pods: bool = True,
) -> list[Pod]:
    """Find populate pods for a given migration.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where populate pods exist.
        migration_uid (str): Migration UID to filter pods by.
        require_pods (bool): When True, raise if no populate pods are found.

    Returns:
        list[Pod]: List of populate pods.

    Raises:
        ValueError: If require_pods is True and no populate pods are found.
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

    if not populate_pods and require_pods:
        raise ValueError(f"No populate pods found for migration '{migration_uid}' in namespace '{namespace}'")

    return populate_pods


_SOURCE_DATASTORE_FROM_LOG_RE = re.compile(
    r'(?:source_vmdk|source)="\[([^\]]+)\]',
)
_XCOPY_USED_LOG_RE = re.compile(r"xcopyUsed=(\d+)")
_COPY_OFFLOAD_FAILED_ERR_RE = re.compile(r'"copy-offload failed" err="([^"]+)"')


def _parse_source_datastore_name_from_log_content(pod_name: str, log_content: str) -> str:
    """Parse the source vSphere datastore display name from populate pod log text.

    Populator logs reference datastores by display name (not MoRef ID), e.g.
    ``source_vmdk="[<datastore-name>] vm-folder/disk.vmdk"``. Callers correlate this
    name to MoRef IDs from provider configuration via ``datastore_names_by_id``.

    Args:
        pod_name (str): Populate pod name (for error messages).
        log_content (str): Full populate pod log text.

    Returns:
        str: Datastore display name from the log.

    Raises:
        ValueError: If no source datastore pattern is found in the pod logs.
    """
    match = _SOURCE_DATASTORE_FROM_LOG_RE.search(log_content)
    if not match:
        raise ValueError(
            f"Source datastore not found in populate pod '{pod_name}' logs "
            '(expected source_vmdk="[<datastore>]..." or source="[<datastore>]...")'
        )
    return match.group(1)


def _parse_xcopy_used_from_log_content(pod_name: str, log_content: str) -> tuple[int, str]:
    """Parse the last xcopyUsed value and its log line from populate pod log text.

    Args:
        pod_name (str): Populate pod name (for error messages).
        log_content (str): Full populate pod log text.

    Returns:
        tuple[int, str]: Last xcopyUsed value (0 or 1) and the log line it appeared on.

    Raises:
        ValueError: If the populator failed before logging xcopyUsed, or xcopyUsed is missing.
    """
    matches: list[re.Match[str]] = list(_XCOPY_USED_LOG_RE.finditer(log_content))
    if not matches:
        failure_match = _COPY_OFFLOAD_FAILED_ERR_RE.search(log_content)
        if failure_match is not None:
            raise ValueError(
                f"Populate pod '{pod_name}' copy-offload failed before xcopyUsed was logged: {failure_match.group(1)}"
            )
        if '"copy-offload failed"' in log_content:
            raise ValueError(
                f"Populate pod '{pod_name}' copy-offload failed before xcopyUsed was logged; "
                "see populate pod logs for details"
            )
        raise ValueError(f"xcopyUsed not found in populate pod '{pod_name}' logs")

    last_match = matches[-1]
    line_start = log_content.rfind("\n", 0, last_match.start()) + 1
    line_end = log_content.find("\n", last_match.end())
    if line_end == -1:
        line_end = len(log_content)
    last_log_line: str = log_content[line_start:line_end].strip()

    return int(last_match.group(1)), last_log_line


def _log_xcopy_verification_result(
    pod_name: str,
    pvc_name: str,
    expected_value: int,
    actual_value: int,
    xcopy_log_line: str,
    *,
    datastore_id: str | None = None,
    datastore_display_name: str | None = None,
) -> None:
    """Log expected vs actual xcopyUsed for a populate pod verification.

    Args:
        pod_name (str): Populate pod name.
        pvc_name (str): PVC label from the pod.
        expected_value (int): Expected xcopyUsed (0 or 1).
        actual_value (int): Actual xcopyUsed parsed from logs.
        xcopy_log_line (str): Log line containing the last xcopyUsed value.
        datastore_id (str | None): Optional MoRef ID when verifying per-datastore.
        datastore_display_name (str | None): Optional vSphere datastore display name.
    """
    pod_context = f"Pod '{pod_name}' (PVC '{pvc_name}'"
    if datastore_id is not None and datastore_display_name is not None:
        pod_context += f", datastore '{datastore_id}' / '{datastore_display_name}'"
    pod_context += ")"

    result_label = "PASS" if expected_value == actual_value else "FAIL"
    LOGGER.info(
        f"{pod_context}: xcopyUsed expected={expected_value} actual={actual_value} "
        f"({result_label}); log: {xcopy_log_line}"
    )


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
        pvc_name: str = pod.instance.metadata.labels.get(PVC_NAME_LABEL, pod.name)
        log_content: str = pod.log()
        xcopy_used, xcopy_log_line = _parse_xcopy_used_from_log_content(
            pod_name=pod.name,
            log_content=log_content,
        )
        _log_xcopy_verification_result(
            pod_name=pod.name,
            pvc_name=pvc_name,
            expected_value=expected_value,
            actual_value=xcopy_used,
            xcopy_log_line=xcopy_log_line,
        )

        assert xcopy_used == expected_value, (
            f"Pod '{pod.name}' (PVC '{pvc_name}'): expected xcopyUsed={expected_value}, "
            f"got xcopyUsed={xcopy_used}; log: {xcopy_log_line}"
        )


def _resolve_datastore_id_from_display_name(
    source_datastore_name: str,
    datastore_names_by_id: dict[str, str],
) -> str:
    """Map a vSphere datastore display name from populator logs to its MoRef ID.

    Args:
        source_datastore_name (str): Datastore display name parsed from populate pod logs.
        datastore_names_by_id (dict[str, str]): Maps each MoRef ID to its vSphere display name.

    Returns:
        str: MoRef ID for the matching datastore.

    Raises:
        ValueError: If the display name does not match any configured datastore.
    """
    matching_ids: list[str] = [
        datastore_id
        for datastore_id, display_name in datastore_names_by_id.items()
        if display_name == source_datastore_name
    ]
    if len(matching_ids) == 1:
        return matching_ids[0]
    if len(matching_ids) > 1:
        raise ValueError(
            f"Datastore display name '{source_datastore_name}' matches multiple configured IDs: {matching_ids}"
        )
    raise ValueError(
        f"Source datastore '{source_datastore_name}' does not match provider-configured datastores "
        f"{datastore_names_by_id}"
    )


def verify_xcopy_used_per_datastore(
    ocp_admin_client: DynamicClient,
    plan: Plan,
    target_namespace: str,
    expected_xcopy_by_datastore_id: dict[str, bool],
    datastore_names_by_id: dict[str, str],
    *,
    require_all_datastores_seen: bool = True,
) -> None:
    """Verify per-disk xcopyUsed based on each disk's source vSphere datastore.

    Use when a migration has disks on multiple datastores with different expected XCOPY
    behavior (e.g. mixed XCOPY-capable and fallback datastores). Provider configuration
    supplies MoRef IDs and expected values; populate pod logs use datastore display names.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
        plan (Plan): The Plan CR resource (used to find the migration UID).
        target_namespace (str): Namespace where populate pods exist.
        expected_xcopy_by_datastore_id (dict[str, bool]): Maps each datastore MoRef ID to
            whether XCOPY is expected (True → xcopyUsed=1, False → xcopyUsed=0).
        datastore_names_by_id (dict[str, str]): Maps each MoRef ID to its vSphere display
            name for correlating populate pod logs. Keys must match
            ``expected_xcopy_by_datastore_id`` exactly.
        require_all_datastores_seen (bool): When True, every configured datastore ID must
            appear in at least one populate pod log. Set False when multiple disks may
            share a datastore and you only need per-pod verification.

    Raises:
        ValueError: If mappings are invalid, populate pods are missing, or a log datastore
            cannot be matched.
        AssertionError: If any disk's xcopyUsed value does not match its datastore expectation.
    """
    if set(expected_xcopy_by_datastore_id.keys()) != set(datastore_names_by_id.keys()):
        raise ValueError(
            "expected_xcopy_by_datastore_id and datastore_names_by_id must have the same keys; "
            f"expected keys {sorted(expected_xcopy_by_datastore_id.keys())}, "
            f"name keys {sorted(datastore_names_by_id.keys())}"
        )

    migration_uid: str = _get_migration_uid(plan=plan)
    LOGGER.info(
        f"Checking per-datastore xcopyUsed for migration '{migration_uid}' "
        f"(datastores: {sorted(expected_xcopy_by_datastore_id.keys())})"
    )

    populate_pods: list[Pod] = _find_populate_pods(
        ocp_admin_client=ocp_admin_client,
        namespace=target_namespace,
        migration_uid=migration_uid,
    )
    LOGGER.info(f"Found {len(populate_pods)} populate pod(s)")

    verified_datastore_ids: set[str] = set()

    for pod in populate_pods:
        pvc_name: str = pod.instance.metadata.labels.get(PVC_NAME_LABEL, pod.name)
        log_content: str = pod.log()
        source_datastore_name: str = _parse_source_datastore_name_from_log_content(
            pod_name=pod.name,
            log_content=log_content,
        )
        source_datastore_id: str = _resolve_datastore_id_from_display_name(
            source_datastore_name=source_datastore_name,
            datastore_names_by_id=datastore_names_by_id,
        )
        expected_xcopy_used: bool = expected_xcopy_by_datastore_id[source_datastore_id]
        expected_value: int = 1 if expected_xcopy_used else 0
        xcopy_used, xcopy_log_line = _parse_xcopy_used_from_log_content(
            pod_name=pod.name,
            log_content=log_content,
        )

        verified_datastore_ids.add(source_datastore_id)
        _log_xcopy_verification_result(
            pod_name=pod.name,
            pvc_name=pvc_name,
            expected_value=expected_value,
            actual_value=xcopy_used,
            xcopy_log_line=xcopy_log_line,
            datastore_id=source_datastore_id,
            datastore_display_name=source_datastore_name,
        )

        assert xcopy_used == expected_value, (
            f"Pod '{pod.name}' (PVC '{pvc_name}', datastore '{source_datastore_id}' / "
            f"'{source_datastore_name}'): expected xcopyUsed={expected_value}, got xcopyUsed={xcopy_used}; "
            f"log: {xcopy_log_line}"
        )

    if require_all_datastores_seen and verified_datastore_ids != set(expected_xcopy_by_datastore_id.keys()):
        raise ValueError(
            "Migration must include at least one disk from each configured datastore; "
            f"verified datastore IDs: {sorted(verified_datastore_ids)}"
        )


def _count_active_populator_pods_by_host(
    ocp_admin_client: DynamicClient,
    namespace: str,
    migration_uid: str,
) -> dict[str, int]:
    """Count active populate pods for a migration grouped by sourceHost label.

    Matches the populator controller's per-host throttling logic: only Running and Pending
    pods with a sourceHost label are counted.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where populate pods exist.
        migration_uid (str): Migration UID to filter populate pods by.

    Returns:
        dict[str, int]: Active populator pod count per ESXi source host.
    """
    counts: dict[str, int] = defaultdict(int)
    for pod in _find_populate_pods(
        ocp_admin_client=ocp_admin_client,
        namespace=namespace,
        migration_uid=migration_uid,
        require_pods=False,
    ):
        pod_status = pod.instance.status
        if not pod_status or pod_status.phase not in _ACTIVE_POPULATOR_POD_PHASES:
            continue
        labels: dict[str, str] = pod.instance.metadata.labels or {}
        source_host: str | None = labels.get(SOURCE_HOST_LABEL)
        if source_host:
            counts[source_host] += 1
    return dict(counts)


class _PopulatorConcurrencyTracker:
    """Track peak populator pod concurrency per ESXi host during migration polling."""

    def __init__(
        self,
        plan: Plan,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        max_populator_inflight: int,
    ) -> None:
        """Initialize tracker state for one migration execution.

        Args:
            plan (Plan): The Plan CR resource defining the migration configuration.
            ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
            target_namespace (str): Namespace where populate pods exist.
            max_populator_inflight (int): Expected ForkliftController populator in-flight limit.
        """
        self._plan = plan
        self._ocp_admin_client = ocp_admin_client
        self._target_namespace = target_namespace
        self._max_populator_inflight = max_populator_inflight
        self._migration_uid: str | None = None
        self._max_concurrent_by_host: dict[str, int] = defaultdict(int)

    def poll(self, _status: str) -> None:
        """Update peak concurrency counters for one migration status poll.

        Args:
            _status (str): Current migration status from ``wait_for_migration_complate``.
        """
        if self._migration_uid is None:
            self._migration_uid = _resolve_migration_uid(plan=self._plan)

        if self._migration_uid is None:
            return

        active_by_host = _count_active_populator_pods_by_host(
            ocp_admin_client=self._ocp_admin_client,
            namespace=self._target_namespace,
            migration_uid=self._migration_uid,
        )
        for source_host, active_count in active_by_host.items():
            self._max_concurrent_by_host[source_host] = max(self._max_concurrent_by_host[source_host], active_count)
            if active_count > self._max_populator_inflight:
                LOGGER.warning(
                    f"Populator concurrency for host '{source_host}' is {active_count} "
                    f"(limit={self._max_populator_inflight})"
                )

    @property
    def results(self) -> dict[str, int]:
        """Peak concurrent active populate pods observed per sourceHost label.

        Returns:
            dict[str, int]: Peak active populator pod count per ESXi source host.
        """
        return dict(self._max_concurrent_by_host)


def execute_migration_monitoring_populator_inflight(
    ocp_admin_client: DynamicClient,
    fixture_store: dict[str, Any],
    plan: Plan,
    target_namespace: str,
    max_populator_inflight: int = POPULATOR_INFLIGHT_LIMIT,
    cut_over: datetime | None = None,
) -> dict[str, int]:
    """Execute a copy-offload migration while tracking peak populator concurrency per host.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
        fixture_store (dict[str, Any]): Fixture store for resource tracking and cleanup.
        plan (Plan): The Plan CR resource defining the migration configuration.
        target_namespace (str): Target namespace for the Migration CR.
        max_populator_inflight (int): Expected ForkliftController populator in-flight limit.
        cut_over (datetime | None): Cut-over datetime for warm migration. Defaults to None.

    Returns:
        dict[str, int]: Peak concurrent active populate pods observed per sourceHost label.

    Raises:
        MigrationPlanExecError: If migration fails or times out.
        TimeoutError: If a copy-offload plan populator secret is not created in time.
    """
    create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Migration,
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        cut_over=cut_over,
    )

    wait_for_copyoffload_plan_secret(
        ocp_admin_client=ocp_admin_client,
        plan=plan,
        namespace=target_namespace,
    )

    tracker = _PopulatorConcurrencyTracker(
        plan=plan,
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        max_populator_inflight=max_populator_inflight,
    )
    wait_for_migration_complate(plan=plan, on_status_poll=tracker.poll)
    return tracker.results


def _verify_source_host_labels_on_pods(populate_pods: list[Pod]) -> str:
    """Verify sourceHost labels on populate pods and return the shared host value.

    Args:
        populate_pods (list[Pod]): Populate pods for the migration.

    Returns:
        str: The shared sourceHost label value.

    Raises:
        ValueError: If labels are missing or inconsistent across pods.
    """
    source_hosts: set[str] = set()
    for pod in populate_pods:
        labels: dict[str, str] = pod.instance.metadata.labels or {}
        source_host: str | None = labels.get(SOURCE_HOST_LABEL)
        if not source_host:
            raise ValueError(f"Populate pod '{pod.name}' is missing required label '{SOURCE_HOST_LABEL}'")
        source_hosts.add(source_host)
        LOGGER.info(f"Populate pod '{pod.name}' has {SOURCE_HOST_LABEL}={source_host!r}")

    if len(source_hosts) != 1:
        raise ValueError(f"Expected a single ESXi sourceHost across populate pods, found: {sorted(source_hosts)}")

    return source_hosts.pop()


def _get_pvc_events(
    ocp_admin_client: DynamicClient,
    namespace: str,
    pvc_name: str,
) -> list[Any]:
    """Return Kubernetes events for a specific PVC.

    Uses an involvedObject field selector, matching the OpenShift console PVC Events tab.
    Do not use ``Event.list()``: it applies a default ``since_seconds=300`` client-side
    filter that drops PopulatorThrottled events emitted at migration start.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        namespace (str): Namespace where the PVC exists.
        pvc_name (str): Name of the PVC to fetch events for.

    Returns:
        list[Any]: Event objects for the PVC from the Kubernetes API.
    """
    event_resource = ocp_admin_client.resources.get(api_version=Event.api_version, kind="Event")
    response = event_resource.get(
        namespace=namespace,
        field_selector=f"involvedObject.name={pvc_name},involvedObject.kind=PersistentVolumeClaim",
    )
    return response.items or []


def _collect_migration_pvc_names(
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    migration_uid: str,
    populate_pods: list[Pod],
) -> tuple[set[str], set[str]]:
    """Collect PVC names from populate pod labels and the migration label selector.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where migration PVCs exist.
        migration_uid (str): Migration UID label value.
        populate_pods (list[Pod]): Populate pods for the migration.

    Returns:
        tuple[set[str], set[str]]: PVC names from pod labels and from the migration label.
    """
    pvc_names_from_pods: set[str] = {
        (pod.instance.metadata.labels or {}).get(PVC_NAME_LABEL, pod.name) for pod in populate_pods
    }
    pvc_names_from_label: set[str] = {
        pvc.name
        for pvc in PersistentVolumeClaim.get(
            client=ocp_admin_client,
            namespace=target_namespace,
            label_selector=f"migration={migration_uid}",
        )
    }
    return pvc_names_from_pods, pvc_names_from_label


def _find_throttled_pvc_names(
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    pvc_names: set[str],
) -> set[str]:
    """Return PVC names that have a PopulatorThrottled event.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where PVC events exist.
        pvc_names (set[str]): PVC names to inspect.

    Returns:
        set[str]: PVC names with at least one PopulatorThrottled event.
    """
    throttled_pvc_names: set[str] = set()
    for pvc_name in pvc_names:
        for event in _get_pvc_events(
            ocp_admin_client=ocp_admin_client,
            namespace=target_namespace,
            pvc_name=pvc_name,
        ):
            if event.get("reason") == POPULATOR_THROTTLED_EVENT_REASON:
                throttled_pvc_names.add(pvc_name)
                LOGGER.info(f"PVC '{pvc_name}' has {POPULATOR_THROTTLED_EVENT_REASON} event")
                break
    return throttled_pvc_names


def _verify_throttled_events_on_pods(
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    migration_uid: str,
    populate_pods: list[Pod],
    max_populator_inflight: int,
) -> None:
    """Verify PopulatorThrottled events on PVCs when the in-flight limit was reached.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace where PVC events exist.
        migration_uid (str): Migration UID for error messages.
        populate_pods (list[Pod]): Populate pods for the migration.
        max_populator_inflight (int): Expected ForkliftController populator in-flight limit.

    Raises:
        ValueError: If populate pod count does not exceed the limit, or too few PVCs have
            PopulatorThrottled events.
    """
    min_expected_throttled = len(populate_pods) - max_populator_inflight
    if min_expected_throttled <= 0:
        raise ValueError(
            f"Expected more populate pods ({len(populate_pods)}) than in-flight limit "
            f"({max_populator_inflight}) to verify throttling for migration '{migration_uid}'"
        )

    pvc_names_from_pods, pvc_names_from_label = _collect_migration_pvc_names(
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        migration_uid=migration_uid,
        populate_pods=populate_pods,
    )
    pvc_names_to_check = pvc_names_from_pods | pvc_names_from_label
    LOGGER.info(
        f"Checking {len(pvc_names_to_check)} PVC(s) for throttled events "
        f"(from pods: {pvc_names_from_pods}, from label: {pvc_names_from_label})"
    )

    throttled_pvc_names = _find_throttled_pvc_names(
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        pvc_names=pvc_names_to_check,
    )

    if len(throttled_pvc_names) < min_expected_throttled:
        raise ValueError(
            f"Expected at least {min_expected_throttled} PVC(s) with {POPULATOR_THROTTLED_EVENT_REASON} "
            f"events for migration '{migration_uid}' ({len(populate_pods)} disks, "
            f"limit={max_populator_inflight}); found {len(throttled_pvc_names)}: {throttled_pvc_names}"
        )
    LOGGER.info(
        f"{len(throttled_pvc_names)}/{len(populate_pods)} PVC(s) reported {POPULATOR_THROTTLED_EVENT_REASON} "
        f"(minimum expected: {min_expected_throttled})"
    )


def verify_populator_throttling(
    ocp_admin_client: DynamicClient,
    plan: Plan,
    target_namespace: str,
    max_concurrent_by_host: dict[str, int],
    max_populator_inflight: int = POPULATOR_INFLIGHT_LIMIT,
) -> str:
    """Verify MTV-696 populator throttling: labels, events, and peak concurrency.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        plan (Plan): The Plan CR resource (used to find the migration UID).
        target_namespace (str): Namespace where populate pods and PVCs exist.
        max_concurrent_by_host (dict[str, int]): Peak concurrent populate pods per sourceHost
            observed during migration.
        max_populator_inflight (int): Expected ForkliftController populator in-flight limit.

    Returns:
        str: The shared sourceHost label value from all populate pods.

    Raises:
        ValueError: If any throttling verification check fails.
    """
    migration_uid, populate_pods = _get_populate_pods_for_plan(
        ocp_admin_client=ocp_admin_client,
        plan=plan,
        target_namespace=target_namespace,
    )
    source_host = _verify_source_host_labels_on_pods(populate_pods=populate_pods)
    _verify_throttled_events_on_pods(
        ocp_admin_client=ocp_admin_client,
        target_namespace=target_namespace,
        migration_uid=migration_uid,
        populate_pods=populate_pods,
        max_populator_inflight=max_populator_inflight,
    )
    if source_host not in max_concurrent_by_host:
        raise ValueError(
            f"No populator monitoring data for sourceHost {source_host!r}; "
            f"observed during migration: {sorted(max_concurrent_by_host)}"
        )
    _verify_populator_inflight_observed(
        max_concurrent_by_host={source_host: max_concurrent_by_host[source_host]},
        max_populator_inflight=max_populator_inflight,
        disk_count=len(populate_pods),
    )
    return source_host


def _verify_populator_inflight_observed(
    max_concurrent_by_host: dict[str, int],
    max_populator_inflight: int = POPULATOR_INFLIGHT_LIMIT,
    disk_count: int | None = None,
) -> None:
    """Verify observed peak populator concurrency respects the configured in-flight limit.

    When ``disk_count`` is provided, also verifies the limit was exercised: peak concurrency
    must reach ``min(max_populator_inflight, disk_count)``. For example, 5 disks with limit 2
    requires peak >= 2, proving two populate pods ran concurrently before others were throttled.

    Args:
        max_concurrent_by_host (dict[str, int]): Peak concurrent populate pods per sourceHost.
        max_populator_inflight (int): Expected ForkliftController populator in-flight limit.
        disk_count (int | None): Total disk/populate-pod count for a single-ESXi-host migration;
            enables minimum peak assertion. Not valid for multi-host migrations.

    Raises:
        ValueError: If no activity was observed, peak exceeds the limit, or peak is below the
            expected minimum when disk_count is set.
    """
    if not max_concurrent_by_host:
        raise ValueError("No populator activity observed during migration monitoring")

    min_expected_peak = min(max_populator_inflight, disk_count) if disk_count is not None else None

    for source_host, peak_count in max_concurrent_by_host.items():
        if peak_count > max_populator_inflight:
            raise ValueError(
                f"Peak populator concurrency for host '{source_host}' was {peak_count}, "
                f"exceeding limit {max_populator_inflight}"
            )
        if min_expected_peak is not None and peak_count < min_expected_peak:
            raise ValueError(
                f"Peak populator concurrency for host '{source_host}' was {peak_count}, "
                f"expected at least {min_expected_peak} "
                f"(limit={max_populator_inflight}, disks={disk_count})"
            )
        LOGGER.info(f"Host '{source_host}': peak populator concurrency {peak_count}/{max_populator_inflight} (PASS)")
