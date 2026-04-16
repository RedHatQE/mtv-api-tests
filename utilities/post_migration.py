from __future__ import annotations

import base64
import ipaddress
import tempfile
from pathlib import Path
from typing import Any

import go_template
import jc
import pytest
from ocp_resources.datavolume import DataVolume
from ocp_resources.network_map import NetworkMap
from ocp_resources.provider import Provider
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from paramiko.ssh_exception import AuthenticationException, ChannelException, NoValidConnectionsError, SSHException
from pyhelper_utils.exceptions import CommandExecFailed
from pytest_testconfig import py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.rhv import OvirtProvider
from utilities.ssh_utils import SSHConnectionManager
from utilities.utils import get_cluster_version, get_value_from_py_config, rhv_provider
from utilities.vmware_guest_operations import DATA_INTEGRITY_FILE

LOGGER = get_logger(name=__name__)

# Kubernetes resource name limits
KUBERNETES_MAX_NAME_LENGTH: int = 63
KUBERNETES_MAX_GENERATE_NAME_PREFIX_LENGTH: int = 58


def get_ssh_credentials_from_provider_config(
    source_provider_data: dict[str, Any], source_vm_info: dict[str, Any]
) -> tuple[str, str]:
    """
    Get SSH credentials from provider configuration based on VM OS type.

    Args:
        source_provider_data: Provider configuration from .providers.json
        source_vm_info: VM information including OS type

    Returns:
        Tuple of (username, password)

    Raises:
        Exception: If credentials are not available for the VM OS type
    """
    # Determine if this is a Windows VM
    is_windows = source_vm_info.get("win_os", False)

    if is_windows:
        # Use Windows credentials
        try:
            username = source_provider_data["guest_vm_win_user"]
            password = source_provider_data["guest_vm_win_password"]
        except KeyError as e:
            raise ValueError(
                f"Windows VM credentials not found in provider config: {e}. "
                "Required: guest_vm_win_user, guest_vm_win_password"
            ) from e
        LOGGER.info(f"Using Windows credentials for VM: {username}")
        return username, password

    # Use Linux credentials
    try:
        username = source_provider_data["guest_vm_linux_user"]
        password = source_provider_data["guest_vm_linux_password"]
    except KeyError as e:
        raise ValueError(
            f"Linux VM credentials not found in provider config: {e}. "
            "Required: guest_vm_linux_user, guest_vm_linux_password"
        ) from e
    LOGGER.info(f"Using Linux credentials for VM: {username}")
    return username, password


def check_ssh_connectivity(
    vm_name: str,
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    source_vm_info: dict[str, Any],
    timeout: int = 300,
    retry_delay: int = 15,
) -> None:
    """
    Test SSH connectivity to a migrated VM using provider credentials with retry support.

    Attempts to establish SSH connectivity with automatic retries on failure. Each attempt
    waits for the host to become connective before considering the test complete.

    Args:
        vm_name: Name of the VM to test
        vm_ssh_connections: SSH connections fixture manager
        source_provider_data: Provider configuration from .providers.json
        source_vm_info: VM information including OS type
        timeout: Maximum time in seconds to retry SSH connectivity (default: 300)
        retry_delay: Delay in seconds between retry attempts (default: 15)

    Raises:
        TimeoutExpiredError: If SSH connectivity cannot be established within the timeout period
    """
    LOGGER.info(f"Testing SSH connectivity to VM {vm_name}")

    # Get credentials from provider config
    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_info)

    # Create SSH connection
    ssh_conn = vm_ssh_connections.create(vm_name=vm_name, username=ssh_username, password=ssh_password)

    def _test_connectivity() -> bool:
        """Test SSH connectivity with retry support.

        Returns:
            bool: True when connectivity is verified; False to retry.
        """
        try:
            with ssh_conn:
                if ssh_conn.is_connective(tcp_timeout=10):
                    LOGGER.info(f"SSH connectivity to VM {vm_name} verified successfully")
                    return True
                LOGGER.warning(f"SSH connectivity test failed for VM {vm_name} - retrying...")
                return False
        except (SSHException, AuthenticationException, NoValidConnectionsError, ChannelException) as e:
            LOGGER.warning(f"SSH connection failed for VM {vm_name}: {type(e).__name__}: {e} - retrying...")
            return False

    try:
        for sample in TimeoutSampler(wait_timeout=timeout, sleep=retry_delay, func=_test_connectivity):
            if sample:
                return
    except TimeoutExpiredError as e:
        raise TimeoutExpiredError(f"SSH connectivity to VM {vm_name} could not be established after {timeout}s") from e


def verify_data_integrity(
    vm_name: str,
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    source_vm_info: dict[str, Any],
    expected_marker_content: str,
    timeout: int = 300,
    retry_delay: int = 15,
) -> None:
    """Verify that data written before migration survived on the migrated VM.

    Reads a marker file created pre-migration and asserts its content matches
    the expected value, confirming disk data integrity through the migration process.

    Args:
        vm_name (str): Name of the migrated VM.
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration with guest credentials.
        source_vm_info (dict[str, Any]): VM information including OS type.
        expected_marker_content (str): The exact string expected in the marker file.
        timeout (int): Maximum seconds to wait for SSH connectivity.
        retry_delay (int): Seconds between SSH retry attempts.

    Raises:
        AssertionError: If marker file content does not match expected value.
        TimeoutExpiredError: If SSH connectivity cannot be established.
        ValueError: If the guest is a Windows VM (Linux-only feature).
    """
    if source_vm_info.get("win_os", False):
        raise ValueError(
            f"Data integrity verification is only supported on Linux guests. "
            f"VM '{vm_name}' is a Windows guest and {DATA_INTEGRITY_FILE!r} is a Linux path."
        )

    LOGGER.info(f"Verifying data integrity on migrated VM {vm_name}")

    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_info)
    ssh_conn = vm_ssh_connections.create(vm_name=vm_name, username=ssh_username, password=ssh_password)

    def _read_marker() -> str | None:
        try:
            with ssh_conn:
                if not ssh_conn.rrmngmnt_host:
                    LOGGER.warning(f"SSH connection not established for VM {vm_name} - retrying...")
                    return None
                executor = ssh_conn.rrmngmnt_host.executor(user=ssh_conn.rrmngmnt_user)
                executor.port = ssh_conn.local_port

                rc, stdout, err = executor.run_cmd(["cat", DATA_INTEGRITY_FILE])
                if rc != 0:
                    LOGGER.warning(f"Failed to read marker file on VM {vm_name}: {err} - retrying...")
                    return None
                return stdout.strip()
        except (SSHException, AuthenticationException, NoValidConnectionsError, ChannelException) as e:
            LOGGER.warning(f"SSH failed for VM {vm_name}: {type(e).__name__}: {e} - retrying...")
            return None

    marker_content: str | None = None
    try:
        for sample in TimeoutSampler(wait_timeout=timeout, sleep=retry_delay, func=_read_marker):
            if sample is not None:
                marker_content = sample
                break
    except TimeoutExpiredError as e:
        raise TimeoutExpiredError(f"Could not read data integrity marker from VM {vm_name} after {timeout}s") from e

    assert marker_content == expected_marker_content, (
        f"Data integrity check failed on VM {vm_name}: expected {expected_marker_content!r}, got {marker_content!r}"
    )
    LOGGER.info(
        f"Data integrity verified on VM {vm_name}: "
        f"marker file {DATA_INTEGRITY_FILE} exists with expected content {marker_content!r}. "
        f"Pre-migration data survived the migration process."
    )


def _parse_windows_network_config(ipconfig_output: str) -> dict[str, dict[str, Any]]:
    """
    Parse Windows ipconfig /all output to extract network interface information.
    Uses the jc library for robust parsing.

    Args:
        ipconfig_output: Output from 'ipconfig /all' command

    Returns:
        Dictionary mapping interface names to their configuration
    """
    # Parse using jc library
    parsed = jc.parse("ipconfig", ipconfig_output)

    interfaces: dict[str, dict[str, Any]] = {}

    for adapter in parsed.get("adapters", []):
        interface_name = adapter.get("name", "unknown")

        ip_addresses: list[dict[str, Any]] = []

        for ipv4 in adapter.get("ipv4_addresses", []):
            ip_addresses.append({
                "ip_address": ipv4.get("address", ""),
                "subnet_mask": ipv4.get("subnet_mask", ""),
                "status": ipv4.get("status", ""),
            })

        interface_config: dict[str, Any] = {
            "name": interface_name,
            "ip_addresses": ip_addresses,
        }

        # Add MAC address if available
        if adapter.get("physical_address"):
            interface_config["macAddress"] = adapter["physical_address"]

        # Add gateway if available (use first one)
        gateways = adapter.get("default_gateways", [])
        if gateways:
            interface_config["gateway"] = gateways[0]

        interfaces[interface_name] = interface_config

    return interfaces


def _parse_linux_network_config(nmcli_output: str) -> dict[str, dict[str, Any]]:
    """Parse Linux 'nmcli device show' output to extract network interface information.

    Args:
        nmcli_output (str): Output from 'nmcli device show' command

    Returns:
        dict[str, dict[str, Any]]: Dictionary mapping interface names to their configuration

    Raises:
        jc.exceptions.ParseError: If jc cannot parse the nmcli output
        KeyError: If jc-parsed device data is missing the required 'device' key
    """
    parsed: list[dict[str, Any]] = jc.parse("nmcli", nmcli_output)
    interfaces: dict[str, dict[str, Any]] = {}

    for device in parsed:
        if device["device"] == "lo" or device.get("state_text") != "connected":
            continue

        cidrs = [v for k, v in device.items() if k.startswith("ip4_address_") and "/" in v]
        if not cidrs:
            continue

        interfaces[device["device"]] = {
            "name": device["device"],
            "ip_addresses": [
                {
                    "ip_address": str(ipaddress.ip_interface(c).ip),
                    "subnet_mask": str(ipaddress.ip_interface(c).netmask),
                    "status": "",
                }
                for c in cidrs
            ],
            **({"macAddress": device["hwaddr"]} if device.get("hwaddr") else {}),
            **({"gateway": device["ip4_gateway"]} if device.get("ip4_gateway") else {}),
        }

    return interfaces


def _extract_static_interfaces(source_vm_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract static IP interfaces from source VM data.

    Args:
        source_vm_data: Source VM data containing network interface information

    Returns:
        List of static interface dictionaries with flattened IP configuration
    """
    static_interfaces = []
    for interface in source_vm_data.get("network_interfaces", []):
        # Check if any IP address in the interface is static
        for ip_config in interface.get("ip_addresses", []):
            if ip_config.get("is_static_ip") is True:
                # Create a flattened interface entry for each static IP
                static_interface = {
                    "name": interface["name"],
                    "macAddress": interface["macAddress"],
                    "network": interface.get("network", {}),
                    "ip_address": ip_config["ip_address"],
                    "subnet_mask": ip_config["subnet_mask"],
                    "gateway": ip_config.get("gateway", ""),
                    "ip_origin": ip_config.get("ip_origin", ""),
                    "is_static_ip": ip_config["is_static_ip"],
                }
                static_interfaces.append(static_interface)
    return static_interfaces


def _verify_subnet_mask(interface_name: str, expected_subnet: str, matching_interface: dict[str, Any]) -> None:
    """
    Verify that the subnet mask matches between source and destination.

    Args:
        interface_name: Name of the network interface
        expected_subnet: Expected subnet mask from source VM
        matching_interface: Current interface configuration from destination VM

    Raises:
        AssertionError: If subnet masks don't match
        ValueError: If subnet masks cannot be compared
    """
    subnet_mask = matching_interface.get("subnet_mask")
    if not subnet_mask:
        raise AssertionError(
            f"Subnet mask not found for interface {interface_name} — "
            f"expected {expected_subnet} but no subnet mask was reported by the guest OS"
        )

    try:
        # Create network objects to compare subnet masks
        expected_network = ipaddress.IPv4Network(f"0.0.0.0/{expected_subnet}", strict=False)
        actual_network = ipaddress.IPv4Network(f"0.0.0.0/{subnet_mask}", strict=False)

        if expected_network.netmask != actual_network.netmask:
            raise AssertionError(
                f"Subnet mask mismatch for interface {interface_name}: expected {expected_subnet} (netmask: "
                f"{expected_network.netmask}), got {subnet_mask} "
                f"(netmask: {actual_network.netmask})"
            )
        else:
            LOGGER.info(f"Subnet mask verified for interface {interface_name}: {expected_subnet} = {subnet_mask}")
    except ValueError as e:
        raise ValueError(
            f"Could not compare subnet masks for interface {interface_name}: {e}. Expected: {expected_subnet}, "
            f"Actual: {subnet_mask}"
        ) from e


def _verify_gateway(interface_name: str, expected_gateway: str, matching_interface: dict[str, Any] | None) -> None:
    """
    Verify that the gateway matches between source and destination (if gateway is configured).

    Args:
        interface_name: Name of the network interface
        expected_gateway: Expected gateway from source VM (may be empty)
        matching_interface: Current interface configuration from destination VM

    Raises:
        AssertionError: If gateways don't match
    """
    if expected_gateway:
        if matching_interface and matching_interface.get("gateway") != expected_gateway:
            raise AssertionError(
                f"Gateway mismatch for interface {interface_name}: expected {expected_gateway}, "
                f"got {matching_interface.get('gateway') if matching_interface else 'None'}"
            )
        else:
            LOGGER.info(f"Gateway verified for interface {interface_name}: {expected_gateway}")
    elif not expected_gateway and matching_interface and matching_interface.get("gateway"):
        LOGGER.warning(
            f"Gateway verification skipped for interface {interface_name}: no gateway in source VM data, but "
            f"destination has {matching_interface.get('gateway')}"
        )


def check_static_ip_preservation(
    vm_name: str,
    vm_ssh_connections: SSHConnectionManager,
    source_vm_data: dict[str, Any],
    source_provider_data: dict[str, Any],
    timeout: int = 600,
    retry_delay: int = 30,
) -> None:
    """
    Verify that static IPs are preserved on the destination VM after migration.
    This function:
    1. Gets static IP configuration from source VM data (IP, subnet mask, static flag)
    2. Connects to destination VM via SSH
    3. Retrieves current network configuration
    4. Validates IP address matches source VM

    Args:
        vm_name: Name of the VM to check
        vm_ssh_connections: SSH connections fixture manager
        source_vm_data: Source VM data collected during plan setup
        source_provider_data: Provider configuration from .providers.json
        timeout: Total timeout in seconds for network configuration retrieval (default: 600)
        retry_delay: Delay in seconds between retry attempts (default: 30)

    Raises:
        Exception: If static IP verification fails
    """
    LOGGER.info(f"Verifying static IP preservation for VM {vm_name}")

    is_windows = source_vm_data.get("win_os", False)
    if is_windows:
        network_cmd = ["ipconfig", "/all"]
    else:
        network_cmd = ["nmcli", "device", "show"]

    # Extract static interfaces
    static_interfaces = _extract_static_interfaces(source_vm_data)

    if not static_interfaces:
        raise ValueError(
            f"preserve_static_ips is enabled but no static IP interfaces found for VM {vm_name}. "
            "Ensure the source VM is powered on so VMware guest tools can report IP origin information."
        )

    LOGGER.info(f"Found {len(static_interfaces)} static IP interfaces to verify")

    # Get SSH credentials
    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_data)

    # Check each static IP interface
    for interface in static_interfaces:
        interface_name = interface.get("name", "unknown")
        expected_ip = interface.get("ip_address", "")
        expected_subnet = interface.get("subnet_mask", "")

        LOGGER.info(f"Verifying interface {interface_name}: IP={expected_ip}, Subnet={expected_subnet}")

        # Get current network configuration via SSH with retry logic It takes time for secondary IPs to appear
        ssh_conn = vm_ssh_connections.create(vm_name=vm_name, username=ssh_username, password=ssh_password)

        def get_matching_interface_with_ip(expected_ip: str = expected_ip) -> dict[str, Any] | None:
            """
            Combined function that:
            1. Gets network configuration
            2. Parses it
            3. Finds matching interface
            4. Checks if expected IP is present

            Returns:
                matching_interface dict if successful
                None if should retry
                Raises CommandExecFailed if permanent failure
            """
            try:
                with ssh_conn:
                    LOGGER.info(f"Attempting to get network configuration using {' '.join(network_cmd)}")

                    # Execute command using executor with the correct user
                    # We need to use the executor with our specific user instead
                    executor = ssh_conn.rrmngmnt_host.executor(user=ssh_conn.rrmngmnt_user)  # type: ignore[union-attr]
                    executor.port = ssh_conn.local_port

                    # Run the command directly
                    rc, stdout, err = executor.run_cmd(network_cmd)
                    if rc != 0:
                        raise CommandExecFailed(name=" ".join(network_cmd), err=err)

                    if not stdout.strip():
                        LOGGER.warning(f"{' '.join(network_cmd)} returned empty output")
                        return None

                    # Parse network configuration
                    try:
                        if is_windows:
                            current_interfaces = _parse_windows_network_config(stdout)
                        else:
                            current_interfaces = _parse_linux_network_config(stdout)
                        LOGGER.info(f"{' '.join(network_cmd)} command executed successfully")
                    except Exception:
                        LOGGER.exception(f"Failed to parse {' '.join(network_cmd)} output")
                        raise

                    # Find matching interface
                    matching_interface = None
                    for iface_name, iface_config in current_interfaces.items():
                        name_match = interface_name.lower() in iface_name.lower()
                        source_mac = interface.get("macAddress", "").lower().replace("-", ":").replace(".", ":")
                        dest_mac = iface_config.get("macAddress", "").lower().replace("-", ":").replace(".", ":")
                        mac_match = source_mac == dest_mac and source_mac != ""

                        if name_match or mac_match:
                            matching_interface = iface_config
                            LOGGER.info(
                                f"Found matching interface: '{iface_name}' (name_match={name_match}, mac_match={mac_match})"
                            )
                            break

                    if not matching_interface:
                        LOGGER.warning(f"Interface {interface_name} not found yet")
                        return None  # Retry

                    # Check if expected IP is found and extract its subnet mask
                    interface_ips = matching_interface.get("ip_addresses", [])

                    for ip_info in interface_ips:
                        if ip_info.get("ip_address") == expected_ip:
                            LOGGER.info(f"SUCCESS: Expected IP {expected_ip} found")
                            matching_interface["subnet_mask"] = ip_info.get("subnet_mask", "")
                            return matching_interface

                    all_ips = [ip_info.get("ip_address") for ip_info in interface_ips]
                    LOGGER.warning(f"Expected IP {expected_ip} not found yet. Found IPs: {all_ips}")
                    return None

            except CommandExecFailed as e:
                # Permanent failure - don't retry
                LOGGER.warning(f"{' '.join(network_cmd)} command failed: {e}")
                raise
            except (SSHException, ChannelException, NoValidConnectionsError, AuthenticationException) as e:
                # Connection issue - retry
                LOGGER.warning(f"SSH connection failed: {e}")
                return None
            except Exception as e:
                # Unexpected error - should not be retried
                LOGGER.error(f"Unexpected error during network config retrieval: {type(e).__name__}: {e}")
                raise

        # Use TimeoutSampler to retry until success
        try:
            matching_interface = None
            for sample in TimeoutSampler(wait_timeout=timeout, sleep=retry_delay, func=get_matching_interface_with_ip):
                if sample:  # Got matching interface with expected IP
                    matching_interface = sample
                    break
        except TimeoutExpiredError as e:
            raise TimeoutError(
                f"Expected IP {expected_ip} not found after {timeout} seconds for interface {interface_name}"
            ) from e
        except CommandExecFailed as e:
            # Re-raise command failures (don't retry)
            raise ValueError(f"Network configuration command failed: {' '.join(network_cmd)} - {e}") from e

        # Verify subnet mask
        if expected_subnet and matching_interface:
            _verify_subnet_mask(interface_name, expected_subnet, matching_interface)

        # Verify gateway
        expected_gateway = interface.get("gateway", "")
        _verify_gateway(interface_name, expected_gateway, matching_interface)

        LOGGER.info(f"Static IP {expected_ip} verified for interface {interface_name}")

    LOGGER.info(f"Static IP preservation verification completed for VM {vm_name}")


def get_destination(map_resource: NetworkMap | StorageMap, source_vm_nic: dict[str, Any]) -> dict[str, Any]:
    """
    Get the source_name's (Network Or Storage) destination_name in a migration map.
    """
    for map_item in map_resource.instance.spec.map:
        result = {"name": "pod"} if map_item.destination.type == "pod" else map_item.destination

        source_vm_network = source_vm_nic["network"]

        if isinstance(source_vm_network, dict):
            source_vm_network = source_vm_network.get("name", source_vm_network.get("id", None))

        if map_item.source.type and map_item.source.type == source_vm_network:
            return result

        if map_item.source.name:
            name_to_compare = (
                map_item.source.name.split("/")[1] if "/" in map_item.source.name else map_item.source.name
            )
            if name_to_compare == source_vm_network:
                return result

        if map_item.source.id and map_item.source.id == source_vm_network:
            return result

    return {}


def check_cpu(source_vm: dict[str, Any], destination_vm: dict[str, Any]) -> None:
    failed_checks = {}

    src_vm_num_cores = source_vm["cpu"]["num_cores"]
    dst_vm_num_cores = destination_vm["cpu"]["num_cores"]

    src_vm_num_sockets = source_vm["cpu"]["num_sockets"]
    dst_vm_num_sockets = destination_vm["cpu"]["num_sockets"]

    if src_vm_num_cores and not src_vm_num_cores == dst_vm_num_cores:
        failed_checks["cpu number of cores"] = (
            f"source_vm cpu cores: {src_vm_num_cores} != destination_vm cpu cores: {dst_vm_num_cores}"
        )

    if src_vm_num_sockets and not src_vm_num_sockets == dst_vm_num_sockets:
        failed_checks["cpu number of sockets"] = (
            f"source_vm cpu sockets: {src_vm_num_sockets} != destination_vm cpu sockets: {dst_vm_num_sockets}"
        )

    if failed_checks:
        raise AssertionError(f"CPU failed checks: {failed_checks}")


def check_memory(source_vm: dict[str, Any], destination_vm: dict[str, Any]) -> None:
    assert source_vm["memory_in_mb"] == destination_vm["memory_in_mb"]


def get_nic_by_mac(nics: list[dict[str, Any]], mac_address: str) -> dict[str, Any]:
    return [nic for nic in nics if nic["macAddress"] == mac_address][0]


def check_network(source_vm: dict[str, Any], destination_vm: dict[str, Any], network_migration_map: NetworkMap) -> None:
    for source_vm_nic in source_vm["network_interfaces"]:
        expected_network = get_destination(network_migration_map, source_vm_nic)

        assert expected_network, "Network not found in migration map"

        expected_network_name = expected_network["name"]

        destination_vm_nic = get_nic_by_mac(
            nics=destination_vm["network_interfaces"], mac_address=source_vm_nic["macAddress"]
        )

        assert destination_vm_nic["network"] == expected_network_name


def check_storage(source_vm: dict[str, Any], destination_vm: dict[str, Any], storage_map_resource: StorageMap) -> None:
    destination_disks = destination_vm["disks"]
    source_vm_disks_storage = [disk["storage"]["name"] for disk in source_vm["disks"]]

    assert len(destination_disks) == len(source_vm["disks"]), "disks count"

    for destination_disk in destination_disks:
        assert destination_disk["storage"]["name"] == py_config["storage_class"], "storage class"
        if destination_disk["storage"]["name"] == "ocs-storagecluster-ceph-rbd":
            for mapping in storage_map_resource.instance.spec.map:
                if mapping.source.name in source_vm_disks_storage:
                    # The following condition is for a customer case (BZ#2064936)
                    if mapping.destination.get("accessMode"):
                        assert destination_disk["storage"]["access_mode"][0] == DataVolume.AccessMode.RWO
                    else:
                        assert destination_disk["storage"]["access_mode"][0] == DataVolume.AccessMode.RWX


def check_pvc_names(
    source_vm: dict[str, Any],
    destination_vm: dict[str, Any],
    pvc_name_template: str | None,
    use_generate_name: bool = False,
    source_provider: BaseProvider | None = None,
    source_provider_inventory: ForkliftInventory | None = None,
) -> None:
    """
    Verify that PVC names match the expected pvcNameTemplate pattern.

    This function:
    1. Orders source disks by their position (controller_key, unit_number)
    2. Verifies the PVC name follows the Forklift template with correct diskIndex

    Args:
        source_vm: Source VM information including disks
        destination_vm: Destination VM information including PVCs
        pvc_name_template: Forklift template string (e.g., "{{.VmName}}-{{.DiskIndex}}")
        use_generate_name: If True, Kubernetes adds random suffix, so use prefix matching
        source_provider: Source provider instance (required for provider-specific validation)
        source_provider_inventory: Forklift inventory for extracting disk filenames (required for {{.FileName}} template)

    Raises:
        AssertionError: If PVC names don't match expected template

    Note:
        Supports full Go template syntax with Sprig functions:
        - {{.VmName}} - VM name
        - {{.DiskIndex}} - Disk index (0-based)
        - {{.FileName}} - VMDK filename without path/extension (VMware only)
        - Sprig functions: mustRegexReplaceAll, replace, lower, upper, etc.

        Examples:
        - "{{.VmName}}-{{.DiskIndex}}"
        - "{{.FileName}}"
        - "{{ .FileName | trimSuffix \".vmdk\" | replace \"_\" \"-\" }}"

        When use_generate_name=True, verifies PVC name starts with template (prefix match).
        When use_generate_name=False, verifies exact name match.
    """
    if not pvc_name_template:
        LOGGER.info("No pvc_name_template specified, skipping PVC name verification")
        return

    # Validate VMware-only wildcards
    for wildcard in ["{{.FileName}}", "{{.DiskIndex}}"]:
        if wildcard in pvc_name_template:
            if not source_provider or source_provider.type != Provider.ProviderType.VSPHERE:
                LOGGER.warning(
                    f"{wildcard} wildcard in pvcNameTemplate is only supported for VMware/vSphere provider. "
                    f"Current provider: {source_provider.type if source_provider else 'unknown'}. "
                    f"Skipping PVC name verification."
                )
                return

    # Get disk filenames from inventory (required for {{.FileName}} template)
    inventory_disk_files: dict[int, str] = {}
    if source_provider_inventory and source_provider:
        try:
            vm_name = source_vm["name"]
            inventory_vm = source_provider_inventory.get_vm(name=vm_name)
            inventory_disks = inventory_vm.get("disks")
            if not inventory_disks:
                LOGGER.warning(f"No disks found in inventory for VM '{vm_name}'")
            else:
                for disk in inventory_disks:
                    if disk.get("file"):
                        # Extract filename from Forklift inventory disk file path
                        # Format: "[datastore1] vm-name/vm-name_1.vmdk"
                        # We extract just the filename: "vm-name_1.vmdk"
                        full_path = disk["file"]
                        if "]" in full_path:
                            full_path = full_path.split("]", 1)[1].strip()
                        filename = full_path.split("/")[-1]
                        inventory_disk_files[disk["key"]] = filename
                LOGGER.debug(f"Got {len(inventory_disk_files)} disk filenames from inventory")
        except (KeyError, ValueError, AttributeError, IndexError) as e:
            LOGGER.warning(f"Could not get disk filenames from inventory: {e}")

    source_disks = source_vm["disks"]
    destination_disks = destination_vm["disks"]

    LOGGER.info(f"Source VM has {len(source_disks)} disks, destination VM has {len(destination_disks)} disks")

    if not source_disks:
        LOGGER.warning("No source disks found for PVC name verification")
        return

    vm_name = source_vm.get("name", "unknown")
    assert destination_disks, (
        f"No destination disks found for VM '{vm_name}'. "
        f"Available keys in destination_vm: {list(destination_vm.keys())}"
    )

    # Sort source disks by their position (controller_key, unit_number)
    # Only VMware has reliable disk ordering metadata
    if source_provider and source_provider.type == Provider.ProviderType.VSPHERE:
        source_disks_ordered = sorted(source_disks, key=lambda d: (d["controller_key"], d["unit_number"]))
    else:
        source_disks_ordered = source_disks

    LOGGER.info(
        f"Verifying PVC names for {len(source_disks_ordered)} disks using Forklift template: '{pvc_name_template}'"
    )
    LOGGER.info(
        f"Source disks (ordered): {
            [
                (d.get('name'), d.get('size_in_kb'), d.get('controller_key'), d.get('unit_number'))
                for d in source_disks_ordered
            ]
        }"
    )
    LOGGER.info(f"Destination disks: {[(d.get('name'), d.get('size_in_kb')) for d in destination_disks]}")

    # Track which destination PVCs we've matched to avoid duplicates
    matched_pvcs = set()

    for source_index, src_disk in enumerate(source_disks_ordered):
        src_name = src_disk["name"]

        # Evaluate Forklift Go template using py-go-template library
        # This supports {{.VmName}}, {{.DiskIndex}}, {{.FileName}} and Sprig functions
        device_key = src_disk.get("device_key")
        if device_key is None:
            LOGGER.warning(f"No device_key found for source disk {source_index} ({src_disk.get('name', 'unknown')})")
            filename = ""
        else:
            filename = inventory_disk_files.get(device_key, "")

        # Warn if FileName template is used but filename not found from inventory
        if "{{.FileName}}" in pvc_name_template and not filename:
            LOGGER.warning(
                f"{{{{.FileName}}}} wildcard used but filename not found for disk with device_key={device_key}. "
                f"Available inventory disk keys: {list(inventory_disk_files.keys())}"
            )

        template_values = {
            "VmName": source_vm["name"],
            "DiskIndex": source_index,
            "FileName": filename,
        }

        # py-go-template requires a file path, so create a temporary file
        tmp_file_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".tmpl", delete=False) as tmp_file:
                tmp_file.write(pvc_name_template)
                tmp_file_path = tmp_file.name

            # Render Go template with proper Sprig function support
            try:
                result = go_template.render(Path(tmp_file_path), template_values)
                expected_pvc_name = result.decode("utf-8") if isinstance(result, bytes) else result
                expected_pvc_name = expected_pvc_name.strip()
            except Exception as template_error:
                raise ValueError(
                    f"Failed to render pvcNameTemplate '{pvc_name_template}' with values {template_values}: {template_error}"
                ) from template_error

            if not expected_pvc_name:
                raise ValueError(
                    f"pvcNameTemplate '{pvc_name_template}' rendered to empty string with values {template_values}"
                )
        finally:
            # Clean up temporary file
            if tmp_file_path:
                Path(tmp_file_path).unlink(missing_ok=True)

        if use_generate_name:
            max_prefix_length = KUBERNETES_MAX_GENERATE_NAME_PREFIX_LENGTH
            if len(expected_pvc_name) > max_prefix_length:
                original_name = expected_pvc_name
                expected_pvc_name = expected_pvc_name[:max_prefix_length]
                LOGGER.info(
                    f"Template result '{original_name}' ({len(original_name)} chars) "
                    f"truncated to '{expected_pvc_name}' (max {max_prefix_length} chars for generateName prefix)"
                )
        else:
            max_name_length = KUBERNETES_MAX_NAME_LENGTH
            if len(expected_pvc_name) > max_name_length:
                original_name = expected_pvc_name
                expected_pvc_name = expected_pvc_name[:max_name_length]
                LOGGER.info(
                    f"Template result '{original_name}' ({len(original_name)} chars) "
                    f"truncated to '{expected_pvc_name}' (max {max_name_length} chars for PVC name)"
                )

        # Find destination PVC that matches the expected name (prefix or exact match)
        matching_pvc = None
        for dest_pvc in destination_disks:
            dest_pvc_name = dest_pvc["name"]
            if dest_pvc_name in matched_pvcs:
                continue

            # Check if this PVC matches the expected name
            if use_generate_name:
                # With generateName, PVC should start with the expected prefix
                if dest_pvc_name.startswith(expected_pvc_name):
                    matching_pvc = dest_pvc
                    matched_pvcs.add(dest_pvc_name)
                    break
            else:
                # Without generateName, PVC should match exactly
                if dest_pvc_name == expected_pvc_name:
                    matching_pvc = dest_pvc
                    matched_pvcs.add(dest_pvc_name)
                    break

        available_pvcs = [d["name"] for d in destination_disks if d["name"] not in matched_pvcs]
        match_type = "prefix" if use_generate_name else "exact name"
        assert matching_pvc, (
            f"No destination PVC found matching {match_type} '{expected_pvc_name}' "
            f"for source disk {source_index} ({src_name}).\n"
            f"  Template: '{pvc_name_template}'\n"
            f"  Expected {'prefix' if use_generate_name else 'name'}: '{expected_pvc_name}'\n"
            f"  Available unmatched PVCs: {available_pvcs}\n"
            f"  Already matched: {matched_pvcs}"
        )

        actual_pvc_name = matching_pvc["name"]

        # Verify disk order: destination unit_number should match source disk index
        # Only for VMware at the moment
        dest_unit_number = matching_pvc.get("unit_number")
        if source_provider and source_provider.type == Provider.ProviderType.VSPHERE:
            assert dest_unit_number is None or dest_unit_number == source_index, (
                f"Disk order mismatch for source disk {source_index} ({src_name}):\n"
                f"  Source disk index: {source_index}\n"
                f"  Destination unit_number: {dest_unit_number}\n"
                f"  PVC name: '{actual_pvc_name}'\n"
                f"  This indicates the disk order was not preserved during migration!"
            )

        # Log successful match
        if use_generate_name:
            LOGGER.info(
                f"Disk {source_index} ({src_name}) -> "
                f"PVC '{actual_pvc_name}' at position {dest_unit_number} "
                f"(matches prefix '{expected_pvc_name}', generateName suffix OK, order preserved)"
            )
        else:
            LOGGER.info(
                f"Disk {source_index} ({src_name}) -> "
                f"PVC '{actual_pvc_name}' at position {dest_unit_number} "
                f"(exact match, order preserved)"
            )

    match_type = "prefix match (generateName=True)" if use_generate_name else "exact match (generateName=False)"
    LOGGER.info(
        f"PVC name verification completed: All {len(source_disks_ordered)} PVC names match template ({match_type})"
    )


def check_vms_power_state(
    source_vm: dict[str, Any],
    destination_vm: dict[str, Any],
    source_power_before_migration: str | None,
    target_power_state: str | None = None,
) -> None:
    # If targetPowerState is specified, check that the destination VM matches it
    if target_power_state:
        actual_power_state = destination_vm["power_state"]
        LOGGER.info(f"Checking target power state: expected={target_power_state}, actual={actual_power_state}")
        assert actual_power_state == target_power_state, (
            f"VM power state mismatch: expected {target_power_state}, got {actual_power_state}"
        )
        LOGGER.info(f"Target power state verification passed: {actual_power_state}")
    elif source_power_before_migration:
        if source_power_before_migration not in ("on", "off"):
            raise ValueError(f"Invalid source_vm_power '{source_power_before_migration}'. Must be 'on' or 'off'")
        # Default behavior: destination VM should match source power state before migration
        assert destination_vm["power_state"] == source_power_before_migration


def check_guest_agent(destination_vm: dict[str, Any]) -> None:
    assert destination_vm.get("guest_agent_running"), "checking guest agent."


def check_false_vm_power_off(source_provider: OvirtProvider, source_vm: dict[str, Any]) -> None:
    """Checking that USER_STOP_VM (event.code=33) was not performed"""
    assert not source_provider.check_for_power_off_event(source_vm["provider_vm_api"]), (
        "Checking RHV VM power off was not performed (event.code=33)"
    )


def check_snapshots(
    snapshots_before_migration: list[dict[str, Any]], snapshots_after_migration: list[dict[str, Any]]
) -> None:
    failed_snapshots: list[str] = []
    snapshots_before_migration.sort(key=lambda x: x["id"])
    snapshots_after_migration.sort(key=lambda x: x["id"])

    time_format: str = "%Y-%m-%d %H:%M"

    for before_snapshot, after_snapshot in zip(snapshots_before_migration, snapshots_after_migration):
        if (
            before_snapshot["create_time"].strftime(time_format) != after_snapshot["create_time"].strftime(time_format)
            or before_snapshot["id"] != after_snapshot["id"]
            or before_snapshot["name"] != after_snapshot["name"]
            or before_snapshot["state"] != after_snapshot["state"]
        ):
            failed_snapshots.append(
                f"snapshot before migration: {before_snapshot}, snapshot after migration: {after_snapshot}"
            )

    if failed_snapshots:
        raise AssertionError(f"Some of the VM snapshots did not match: {failed_snapshots}")


def _format_uuid_to_vmware_serial(uuid: str) -> str:
    """
    Format a UUID to VMware BIOS serial format.

    Converts: "12345678-1234-1234-1234-123456789012"
    To: "VMware-12 34 56 78 12 34 12 34-12 34 12 34 56 78 90 12"

    Args:
        uuid: UUID string with hyphens

    Returns:
        Formatted VMware BIOS serial string
    """
    uuid_no_hyphens = uuid.replace("-", "").upper()
    return (
        f"VMware-{' '.join([uuid_no_hyphens[i : i + 2] for i in range(0, 16, 2)])}-"
        f"{' '.join([uuid_no_hyphens[i : i + 2] for i in range(16, 32, 2)])}"
    )


def check_serial_preservation(
    source_vm: dict[str, Any], destination_vm: dict[str, Any], destination_provider: BaseProvider
) -> None:
    """
    Verify that the VM serial number is preserved during migration from VMware to OpenShift.

    Behavior depends on OpenShift version:
    - OCP 4.20+: UUID is formatted as BIOS serial (VMware-XX XX XX...)
    - Before OCP 4.20: UUID is used as-is

    Args:
        source_vm: Source VM information including uuid
        destination_vm: Destination VM information including serial
        destination_provider: OpenShift destination provider for version detection

    Raises:
        AssertionError: If serial number validation fails
        ValueError: If OCP version cannot be determined or parsed
    """
    source_uuid = source_vm["uuid"]
    dest_serial = destination_vm["serial"]
    vm_name = destination_vm["name"]

    # Validate serial number exists and is a string
    assert dest_serial and isinstance(dest_serial, str), (
        f"Destination VM {vm_name} has no valid serial number in firmware spec (got: {dest_serial})"
    )

    # Validate destination provider has ocp_resource
    ocp_resource = destination_provider.ocp_resource
    if not ocp_resource:
        raise ValueError("Destination provider has no ocp_resource, cannot determine OCP version")

    # Get OCP version to determine expected behavior
    ocp_version = get_cluster_version(ocp_resource.client)

    # Extract major and minor version (ignore patch and pre-release)
    major = ocp_version.major
    minor = ocp_version.minor

    # Check if version is 4.20 or newer (including rc versions)
    is_ocp_420_or_newer = (major > 4) or (major == 4 and minor >= 20)

    comparison = ">=" if is_ocp_420_or_newer else "<"
    uuid_format = "formatted" if is_ocp_420_or_newer else "plain"
    LOGGER.info(f"OCP version {ocp_version} (major={major}, minor={minor}) {comparison} 4.20: Using {uuid_format} UUID")

    # Generate expected serial formats
    expected_serial_420 = _format_uuid_to_vmware_serial(source_uuid)
    expected_serial_pre420 = source_uuid

    # Check based on version
    if is_ocp_420_or_newer:
        # OCP 4.20+: Expect formatted serial
        assert str(dest_serial).lower() == expected_serial_420.lower(), (
            f"Serial number mismatch for VM {vm_name} (OCP {ocp_version} >= 4.20):\n"
            f"  Source UUID: {source_uuid}\n"
            f"  Expected formatted serial: {expected_serial_420}\n"
            f"  Actual destination serial: {dest_serial}"
        )
        LOGGER.info(f"Serial preserved correctly (OCP {ocp_version} >= 4.20, formatted): {dest_serial}")
    else:
        # OCP < 4.20: Expect plain UUID
        assert str(dest_serial).lower() == expected_serial_pre420.lower(), (
            f"Serial number mismatch for VM {vm_name} (OCP {ocp_version} < 4.20):\n"
            f"  Source UUID: {source_uuid}\n"
            f"  Expected plain UUID: {expected_serial_pre420}\n"
            f"  Actual destination serial: {dest_serial}"
        )
        LOGGER.info(f"Serial preserved correctly (OCP {ocp_version} < 4.20, plain UUID): {dest_serial}")


def check_vm_node_placement(
    destination_vm: dict[str, Any],
    expected_node: str,
) -> None:
    """Verify VM is scheduled on the expected labeled node.

    Args:
        destination_vm: Destination VM information including node_name
        expected_node: Expected node name where VM should be scheduled

    Raises:
        AssertionError: If VM has no node assignment or is not on the expected node
    """
    vm_name = destination_vm.get("name")
    actual_node = destination_vm.get("node_name")

    if not actual_node:
        raise AssertionError(f"VM {vm_name} has no node assignment")

    if actual_node != expected_node:
        raise AssertionError(
            f"VM {vm_name} not scheduled on expected node. Expected: {expected_node}, Got: {actual_node}"
        )

    LOGGER.info(f"VM {vm_name} correctly scheduled on node {actual_node}")


def check_vm_labels(
    destination_vm: dict[str, Any],
    expected_labels: dict[str, str],
) -> None:
    """Verify VM has the expected labels set on its metadata.

    Args:
        destination_vm: Destination VM information including labels
        expected_labels: Expected labels that should be set on the VM

    Raises:
        AssertionError: If VM has no labels or labels don't match expected values
    """
    from ocp_resources.resource import ResourceField  # noqa: PLC0415

    vm_name = destination_vm.get("name")
    actual_labels_raw: ResourceField | None = destination_vm.get("labels")

    # Convert ResourceField to dict
    # Kubernetes API returns ResourceField objects. Use .to_dict() for recursive conversion
    # (handles nested ResourceField objects). The 'in' operator doesn't work on ResourceField.
    actual_labels: dict[str, str] = actual_labels_raw.to_dict() if actual_labels_raw else {}

    # Fail if VM has no labels but we expect some
    if not actual_labels:
        raise AssertionError(f"VM {vm_name} has no labels but expected: {expected_labels}")

    missing_labels = []
    incorrect_labels = []

    for label_key, expected_value in expected_labels.items():
        if label_key not in actual_labels:
            missing_labels.append(f"{label_key}=<missing>")
        elif actual_labels[label_key] != expected_value:
            incorrect_labels.append(f"{label_key}={actual_labels[label_key]} (expected: {expected_value})")

    if missing_labels or incorrect_labels:
        error_msg = f"VM {vm_name} label verification failed:\n"
        if missing_labels:
            error_msg += f"  Missing labels: {', '.join(missing_labels)}\n"
        if incorrect_labels:
            error_msg += f"  Incorrect labels: {', '.join(incorrect_labels)}\n"
        error_msg += f"  Actual labels: {actual_labels}\n"
        error_msg += f"  Expected labels: {expected_labels}"
        raise AssertionError(error_msg)

    LOGGER.info(f"VM {vm_name} labels verified successfully: {actual_labels}")


def check_vm_affinity(
    destination_vm: dict[str, Any],
    expected_affinity: dict[str, Any],
) -> None:
    """Check VM affinity matches expected configuration.

    Args:
        destination_vm: VM info dict from provider
        expected_affinity: Expected affinity configuration dict

    Raises:
        AssertionError: If VM has no affinity configuration or affinity doesn't match
    """
    from ocp_resources.resource import ResourceField  # noqa: PLC0415

    vm_name = destination_vm.get("name")
    actual_affinity_raw: ResourceField | None = destination_vm.get("affinity")

    # Convert ResourceField to dict
    # Kubernetes API returns nested ResourceField objects. Must use .to_dict() for recursive conversion.
    # Using dict() only converts top level, leaving nested ResourceField objects that break comparison.
    actual_affinity: dict[str, Any] = actual_affinity_raw.to_dict() if actual_affinity_raw else {}

    if not actual_affinity:
        raise AssertionError(f"VM {vm_name} has no affinity configuration")

    # Deep comparison of affinity configurations
    if actual_affinity != expected_affinity:
        raise AssertionError(
            f"VM {vm_name} affinity verification failed:\n"
            f"  Expected affinity: {expected_affinity}\n"
            f"  Actual affinity: {actual_affinity}"
        )

    LOGGER.info(f"VM {vm_name} affinity verified successfully: {actual_affinity}")


def check_ssl_configuration(source_provider: BaseProvider) -> None:
    """
    Verify that Provider secret's insecureSkipVerify matches the global configuration.

    This ensures that when source_provider_insecure_skip_verify is set to false, the Provider is actually
    configured to verify SSL certificates (and vice versa).

    Args:
        source_provider: The source provider to check

    Raises:
        AssertionError: If insecureSkipVerify doesn't match the configuration
    """
    # Get the expected value from config
    insecure_config = get_value_from_py_config("source_provider_insecure_skip_verify")
    expected_value = "true" if insecure_config else "false"

    LOGGER.info(f"Checking SSL configuration: expected insecureSkipVerify='{expected_value}'")

    assert source_provider.ocp_resource is not None

    provider_secret_ref = source_provider.ocp_resource.instance.spec.secret
    if not provider_secret_ref:
        LOGGER.warning("Provider has no secret reference, skipping SSL verification")
        return

    assert provider_secret_ref.name is not None
    assert provider_secret_ref.namespace is not None

    secret = Secret(
        client=source_provider.ocp_resource.client,
        name=provider_secret_ref.name,
        namespace=provider_secret_ref.namespace,
    )

    # Check insecureSkipVerify field exists and has a value
    assert secret.instance.data.get("insecureSkipVerify"), "Provider secret is missing 'insecureSkipVerify' field"

    actual_value = base64.b64decode(secret.instance.data["insecureSkipVerify"]).decode("utf-8")

    config_str_value = py_config.get("source_provider_insecure_skip_verify")
    assert actual_value == expected_value, (
        f"SSL configuration mismatch: config has source_provider_insecure_skip_verify='{config_str_value}', "
        f"but Provider secret has insecureSkipVerify='{actual_value}' (expected '{expected_value}')"
    )

    LOGGER.info(f"SSL configuration verified: insecureSkipVerify='{actual_value}' matches config")


def check_vms(
    plan: dict[str, Any],
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    network_map_resource: NetworkMap,
    storage_map_resource: StorageMap,
    source_provider_data: dict[str, Any],
    source_vms_namespace: str,
    source_provider_inventory: ForkliftInventory | None = None,
    vm_ssh_connections: SSHConnectionManager | None = None,
    labeled_worker_node: dict[str, Any] | None = None,
    target_vm_labels: dict[str, Any] | None = None,
) -> None:
    res: dict[str, list[str]] = {}

    # Use custom VM namespace (always set by prepared_plan fixture)
    vm_namespace = plan["_vm_target_namespace"]

    # Verify SSL configuration matches the global setting (VMware, RHV, OpenStack)
    if source_provider.type in (
        Provider.ProviderType.VSPHERE,
        Provider.ProviderType.RHV,
        Provider.ProviderType.OPENSTACK,
    ):
        try:
            check_ssl_configuration(source_provider=source_provider)
        except Exception as exp:
            LOGGER.error(f"SSL configuration check failed: {exp}")
            res.setdefault("_provider", []).append(f"check_ssl_configuration - {str(exp)}")

    for vm in plan["virtual_machines"]:
        vm_name = vm["name"]
        destination_vm_name = vm.get("targetName", vm_name)
        res[vm_name] = []

        source_vm = source_provider.vm_dict(
            name=vm_name,
            namespace=source_vms_namespace,
            source=True,
            source_provider_inventory=source_provider_inventory,
        )
        vm_guest_agent = vm.get("guest_agent")
        vm_kwargs = {
            "wait_for_guest_agent": vm_guest_agent,
            "name": destination_vm_name,
            "namespace": vm_namespace,
        }
        if (guest_agent_timeout := plan.get("guest_agent_timeout")) is not None:
            vm_kwargs["guest_agent_timeout"] = guest_agent_timeout
        destination_vm = destination_provider.vm_dict(**vm_kwargs)

        # Group 1: All providers — destination checks
        try:
            check_vms_power_state(
                source_vm=source_vm,
                destination_vm=destination_vm,
                source_power_before_migration=vm.get("source_vm_power"),
                target_power_state=plan.get("target_power_state"),
            )
        except Exception as exp:
            res[vm_name].append(f"check_vms_power_state - {str(exp)}")

        if vm_guest_agent:
            try:
                check_guest_agent(destination_vm=destination_vm)
            except Exception as exp:
                res[vm_name].append(f"check_guest_agent - {str(exp)}")

        # SSH connectivity check - only when destination VM is powered on
        if vm_ssh_connections and destination_vm.get("power_state") == "on":
            try:
                check_ssh_connectivity(
                    vm_name=destination_vm_name,
                    vm_ssh_connections=vm_ssh_connections,
                    source_provider_data=source_provider_data,
                    source_vm_info=source_vm,
                )
            except Exception as exp:
                res[vm_name].append(f"check_ssh_connectivity - {str(exp)}")

            # Static IP preservation check - for VMs with preserve_static_ips enabled, migrated from VSPHERE
            source_vm_data = plan.get("source_vms_data", {}).get(vm["name"], {})

            # Fail fast: if preserve_static_ips is requested for vSphere, source_vms_data must exist
            if plan.get("preserve_static_ips") and source_provider.type == Provider.ProviderType.VSPHERE:
                if not source_vm_data:
                    raise ValueError(
                        f"preserve_static_ips is enabled but source_vms_data is missing for VM '{vm['name']}'. "
                        "Ensure the prepared_plan fixture populates source_vms_data for static IP verification."
                    )

            if (
                source_vm_data
                and plan.get("preserve_static_ips")
                and source_provider.type == Provider.ProviderType.VSPHERE
            ):
                try:
                    check_static_ip_preservation(
                        vm_name=destination_vm_name,
                        vm_ssh_connections=vm_ssh_connections,
                        source_vm_data=source_vm_data,
                        source_provider_data=source_provider_data,
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_static_ip_preservation - {str(exp)}")
        elif vm_ssh_connections:
            LOGGER.info(
                f"Skipping SSH connectivity check for VM {vm_name} - destination VM is not powered on "
                f"(power_state: {destination_vm.get('power_state', 'unknown')})"
            )

        if plan.get("target_node_selector") and labeled_worker_node:
            try:
                check_vm_node_placement(
                    destination_vm=destination_vm,
                    expected_node=labeled_worker_node["node_name"],
                )
            except Exception as exp:
                res[vm_name].append(f"check_vm_node_placement - {str(exp)}")

        if plan.get("target_labels") and target_vm_labels:
            try:
                check_vm_labels(
                    destination_vm=destination_vm,
                    expected_labels=target_vm_labels["vm_labels"],
                )
            except Exception as exp:
                res[vm_name].append(f"check_vm_labels - {str(exp)}")

        if plan.get("target_affinity"):
            try:
                check_vm_affinity(
                    destination_vm=destination_vm,
                    expected_affinity=plan["target_affinity"],
                )
            except Exception as exp:
                res[vm_name].append(f"check_vm_affinity - {str(exp)}")

        # Group 2: Source-comparison checks — require real source VM data (OVA has none)
        if source_provider.type != Provider.ProviderType.OVA:
            try:
                check_cpu(source_vm=source_vm, destination_vm=destination_vm)
            except Exception as exp:
                res[vm_name].append(f"check_cpu - {str(exp)}")

            try:
                check_memory(source_vm=source_vm, destination_vm=destination_vm)
            except Exception as exp:
                res[vm_name].append(f"check_memory - {str(exp)}")

            # TODO: Remove when OCP to OCP migration is done with 2 clusters
            if source_provider.type != Provider.ProviderType.OPENSHIFT:
                try:
                    check_network(
                        source_vm=source_vm,
                        destination_vm=destination_vm,
                        network_migration_map=network_map_resource,
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_network - {str(exp)}")

            try:
                check_storage(
                    source_vm=source_vm, destination_vm=destination_vm, storage_map_resource=storage_map_resource
                )
            except Exception as exp:
                res[vm_name].append(f"check_storage - {str(exp)}")

            if plan.get("pvc_name_template"):
                try:
                    check_pvc_names(
                        source_vm=plan.get("source_vms_data", {}).get(vm["name"], source_vm),
                        destination_vm=destination_vm,
                        pvc_name_template=plan["pvc_name_template"],
                        use_generate_name=plan.get("pvc_name_template_use_generate_name", False),
                        source_provider=source_provider,
                        source_provider_inventory=source_provider_inventory,
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_pvc_names - {str(exp)}")
        else:
            LOGGER.info(f"Skipping source-comparison checks for OVA VM '{vm_name}' (no source stats)")

        # Group 3: vSphere-specific checks
        if source_provider.type == Provider.ProviderType.VSPHERE:
            if snapshots_before_migration := vm.get("snapshots_before_migration"):
                try:
                    check_snapshots(
                        snapshots_before_migration=snapshots_before_migration,
                        snapshots_after_migration=source_vm["snapshots_data"],
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_snapshots - {str(exp)}")

            try:
                check_serial_preservation(
                    source_vm=source_vm, destination_vm=destination_vm, destination_provider=destination_provider
                )
            except Exception as exp:
                res[vm_name].append(f"check_serial_preservation - {str(exp)}")

        # Group 4: RHV-specific checks
        if rhv_provider(source_provider_data) and isinstance(source_provider, OvirtProvider):
            try:
                check_false_vm_power_off(source_provider=source_provider, source_vm=source_vm)
            except Exception as exp:
                res[vm_name].append(f"check_false_vm_power_off - {str(exp)}")

    failed_checks = {vm_name: errors for vm_name, errors in res.items() if errors}
    if failed_checks:
        failure_details = "; ".join(f"{vm_name}: [{', '.join(errors)}]" for vm_name, errors in failed_checks.items())
        pytest.fail(f"VM validation failed — {failure_details}")
