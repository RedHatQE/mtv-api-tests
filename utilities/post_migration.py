from __future__ import annotations

import base64
import ipaddress
import json
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import go_template
import jc
import pytest
from kubernetes.dynamic.exceptions import NotFoundError
from ocp_resources.datavolume import DataVolume
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.network_map import NetworkMap
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.plan import Plan
from ocp_resources.provider import Provider
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine import VirtualMachine
from paramiko.ssh_exception import AuthenticationException, ChannelException, NoValidConnectionsError, SSHException
from pyhelper_utils.exceptions import CommandExecFailed
from pytest_testconfig import py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.rhv import OvirtProvider
from utilities.deep_inspection import verify_captured_di_results
from utilities.naming import resolve_destination_vm_name
from utilities.ssh_utils import SSHConnectionManager, VMSSHConnection, run_cmd_in_vm
from utilities.utils import get_cluster_version, get_value_from_py_config, normalize_source_network, rhv_provider
from utilities.vmware_guest_operations import DATA_INTEGRITY_FILE

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.providers.openshift import OCPProvider

# Forklift aligns DataVolume capacity up to this block size (1 MiB) when sizing destination PVCs.
_DATAVOLUME_ALIGN_BLOCK_BYTES = 1024 * 1024

_DISK_SERIAL_MAX = 20  # libvirt disk serial limit; forklift writes the source disk UUID prefix

_CDI_FILESYSTEM_OVERHEAD = 0.055  # CDI default filesystem overhead fraction for Filesystem-mode volumes
_PVC_REQUEST_ROUNDING_SLACK_KB = 1024 * 1024  # 1 GiB slack for provisioner rounding of PVC requests to whole GiB

LOGGER = get_logger(name=__name__)

# Kubernetes resource name limits
KUBERNETES_MAX_NAME_LENGTH: int = 63
KUBERNETES_MAX_GENERATE_NAME_PREFIX_LENGTH: int = 58

_LUKS_FSTYPE = "crypto_LUKS"  # lsblk filesystem-type identifier for LUKS partitions

# Providers supporting static IP preservation verification.
_STATIC_IP_PROVIDERS: tuple[str, ...] = (Provider.ProviderType.VSPHERE, Provider.ProviderType.HYPERV)

_VBS_STATUS_RUNNING = "2"
_NESTED_VIRT_DISABLED_FEATURES: list[dict[str, str]] = [
    {"name": "vmx", "policy": "disable"},
    {"name": "svm", "policy": "disable"},
]

# Well-known name of the ForkliftController CR created by the MTV operator
_FORKLIFT_CONTROLLER_NAME = "forklift-controller"

# Session-level cache for is_multus_network_name_always_qualified(); tests never modify this flag.
_multus_network_name_always_qualified_cache: bool | None = None

_WINDOWS_NETWORK_CMD = ["ipconfig", "/all"]


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


def _find_luks_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively search lsblk blockdevice tree for LUKS-encrypted devices.

    Args:
        devices (list[dict[str, Any]]): List of blockdevice dicts from lsblk -J -f output.

    Returns:
        list[dict[str, Any]]: Blockdevice entries with fstype == "crypto_LUKS".
    """
    found: list[dict[str, Any]] = []
    for dev in devices:
        if dev.get("fstype") == _LUKS_FSTYPE:
            found.append(dev)
        found.extend(_find_luks_devices(dev.get("children") or []))
    return found


def verify_luks_encryption(
    vm_name: str,
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    source_vm_info: dict[str, Any],
    timeout: int = 300,
    retry_delay: int = 15,
) -> None:
    """Verify that LUKS disk encryption is active on the migrated VM.

    SSHs into the migrated VM, runs lsblk -J -f for structured JSON output,
    and searches the blockdevice tree for crypto_LUKS entries.

    Args:
        vm_name (str): Name of the migrated VM.
        vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        source_provider_data (dict[str, Any]): Provider configuration with guest credentials.
        source_vm_info (dict[str, Any]): VM information including OS type.
        timeout (int): Maximum seconds to wait for SSH connectivity.
        retry_delay (int): Seconds between SSH retry attempts.

    Raises:
        AssertionError: If no LUKS-encrypted devices are found.
        TimeoutExpiredError: If SSH connectivity cannot be established.
    """
    LOGGER.info(f"Verifying LUKS encryption on migrated VM {vm_name}")

    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_info)
    ssh_conn = vm_ssh_connections.create(vm_name=vm_name, username=ssh_username, password=ssh_password)

    def _check_luks() -> list[dict[str, Any]] | None:
        try:
            with ssh_conn:
                if not ssh_conn.rrmngmnt_host:
                    LOGGER.warning(f"SSH connection not established for VM {vm_name} - retrying...")
                    return None
                executor = ssh_conn.rrmngmnt_host.executor(user=ssh_conn.rrmngmnt_user)
                executor.port = ssh_conn.local_port
                rc, stdout, err = executor.run_cmd(["lsblk", "-J", "-f"])
                if rc != 0:
                    LOGGER.warning(f"lsblk failed on VM {vm_name}: {err} - retrying...")
                    return None
                try:
                    lsblk_data = json.loads(stdout)
                except json.JSONDecodeError as e:
                    LOGGER.warning(f"Invalid lsblk JSON on VM {vm_name}: {e} - retrying...")
                    return None

                return _find_luks_devices(lsblk_data["blockdevices"])
        except (SSHException, AuthenticationException, NoValidConnectionsError, ChannelException) as e:
            LOGGER.warning(f"SSH failed for VM {vm_name}: {type(e).__name__}: {e} - retrying...")
            return None

    # None = transient failure (retry); empty list = no LUKS devices (definitive → assert)
    luks_devices: list[dict[str, Any]] | None = None
    try:
        for sample in TimeoutSampler(wait_timeout=timeout, sleep=retry_delay, func=_check_luks):
            if sample is not None:
                luks_devices = sample
                break
    except TimeoutExpiredError as e:
        raise TimeoutExpiredError(f"Could not verify LUKS encryption on VM {vm_name} after {timeout}s") from e

    assert luks_devices, f"No LUKS-encrypted devices found on migrated VM {vm_name}"
    LOGGER.info(f"LUKS encryption verified on VM {vm_name}: {len(luks_devices)} encrypted device(s) found")


def _normalize_windows_command_output(output: str) -> str:
    """Normalize Windows SSH command output for jc parsing.

    The Windows OpenSSH exec channel may deliver UTF-16-mangled text (BOM and NUL
    bytes) or CRLF line endings, which makes jc fail to recognize the output as
    Windows ipconfig ("command output from this OS (linux) is not supported").

    Args:
        output: Raw command output string

    Returns:
        Normalized string: BOM and NUL bytes stripped, line endings unified to LF
    """
    return output.lstrip("\ufeff").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


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
    parsed = jc.parse("ipconfig", _normalize_windows_command_output(ipconfig_output))

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

        # Add DHCP status if reported (absent on some loopback-like adapters)
        if adapter.get("dhcp_enabled") is not None:
            interface_config["dhcp_enabled"] = adapter["dhcp_enabled"]

        # Add DNS servers if reported
        if adapter.get("dns_servers"):
            interface_config["dns_servers"] = adapter["dns_servers"]

        # Add gateways if available (use first one as primary)
        gateways = adapter.get("default_gateways", [])
        if gateways:
            interface_config["gateway"] = gateways[0]
            interface_config["default_gateways"] = gateways

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
            "dns_servers": [v for k, v in device.items() if k.startswith("ip4_dns_")],
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
                    "dns_servers": ip_config.get("dns_servers", []),
                    "ip_origin": ip_config.get("ip_origin", ""),
                    "is_static_ip": ip_config["is_static_ip"],
                }
                static_interfaces.append(static_interface)
    return static_interfaces


def _is_pod_mapped_static_interface(
    # dict values are heterogeneous runtime provider data (bools, IP strings, nested
    # network dicts) — the file-wide dict[str, Any] convention for external data.
    static_interface: dict[str, Any],
    destination_vm: dict[str, Any],
    network_map_resource: NetworkMap,
) -> bool:
    """Check whether a static-IP interface landed on pod networking after migration.

    The product does not preserve static IPs on pod-mapped interfaces, so they are
    excluded from static-IP verification. With several destinations (per-NIC mapping),
    the actual binding is resolved from the destination VM's NIC with the same MAC.

    Args:
        static_interface (dict[str, Any]): Flattened static-IP interface entry from _extract_static_interfaces
        destination_vm (dict[str, Any]): Destination VM data whose NICs carry the landed network names
        network_map_resource (NetworkMap): NetworkMap resource resolving source networks to destinations

    Returns:
        True if the interface is attached to the pod network on the destination VM
    """
    destinations = get_all_destinations(network_map_resource, static_interface)
    if destinations == ["pod"]:
        return True
    if "pod" in destinations:
        destination_nic = get_nic_by_mac(
            nics=destination_vm["network_interfaces"], mac_address=static_interface["macAddress"]
        )
        return destination_nic["network"] == "pod"
    return False


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


def _verify_dns_servers(
    interface_name: str,
    expected_dns_servers: list[str],
    matching_interface: dict[str, Any],
    is_windows: bool,
) -> None:
    """
    Verify that the destination interface's DNS servers match the source VM's DNS set.

    On Linux, DNS is collected per NIC from NetworkManager profiles, so the sets
    must match exactly. VMware Tools reports Windows' system-wide DNS resolver
    list, not per-NIC configuration, so only DNS servers missing from the
    source set indicate a change on the destination.

    Args:
        interface_name (str): Name of the network interface
        expected_dns_servers (list[str]): DNS servers reported by the source VM (empty skips verification)
        matching_interface (dict[str, Any]): Current interface configuration from destination VM
        is_windows (bool): Whether the guest is Windows (system-wide DNS union instead of per-NIC DNS)

    Raises:
        AssertionError: If the destination DNS set is empty or differs from the source VM's
    """
    if not expected_dns_servers:
        return

    actual_dns_servers = matching_interface.get("dns_servers", [])
    difference = (
        set(actual_dns_servers) - set(expected_dns_servers)
        if is_windows
        else set(actual_dns_servers) ^ set(expected_dns_servers)
    )
    assert actual_dns_servers and not difference, (
        f"DNS servers not preserved for interface {interface_name}: source VM DNS set "
        f"{expected_dns_servers}, got {actual_dns_servers}"
    )
    LOGGER.info(f"DNS servers verified for interface {interface_name}: {actual_dns_servers}")


def load_current_interfaces(
    ssh_conn: VMSSHConnection,
    network_cmd: list[str],
    is_windows: bool,
    vm_name: str,
) -> dict[str, dict[str, Any]] | None:
    """Run a network command over SSH and parse the guest's interface configuration.

    Args:
        ssh_conn (VMSSHConnection): SSH connection to the destination VM
        network_cmd (list[str]): Command to run (e.g. ['nmcli', 'device', 'show'])
        is_windows (bool): True to parse ipconfig output, False for nmcli
        vm_name (str): VM name used in log messages

    Returns:
        dict[str, dict[str, Any]] | None: Parsed interfaces keyed by name, or None to signal retry

    Raises:
        CommandExecFailed: If the network command exits with a non-zero code
    """
    if not ssh_conn.rrmngmnt_host:
        LOGGER.warning(f"SSH connection not established for VM {vm_name} - retrying...")
        return None

    executor = ssh_conn.rrmngmnt_host.executor(user=ssh_conn.rrmngmnt_user)
    executor.port = ssh_conn.local_port
    rc, stdout, err = executor.run_cmd(network_cmd)
    if rc != 0:
        raise CommandExecFailed(name=" ".join(network_cmd), err=err)

    if not stdout.strip():
        LOGGER.warning(f"{' '.join(network_cmd)} returned empty output")
        return None

    return _parse_windows_network_config(stdout) if is_windows else _parse_linux_network_config(stdout)


def _match_expected_static_ips(
    current_interfaces: dict[str, dict[str, Any]],
    static_interfaces: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Match expected static-IP interfaces against the guest's current interfaces.

    Uses a phased matching priority: exact MAC match first, then exact name match,
    then unique substring name match (ambiguous substring matches are skipped).

    Args:
        current_interfaces (dict[str, dict[str, Any]]): Parsed guest interfaces keyed by name
        static_interfaces (list[dict[str, Any]]): Expected static-IP entries from source VM data

    Returns:
        tuple[list[dict[str, Any]], list[str]]: A pair of (matched results, missing descriptions).
            Each matched result is a shallow copy of the interface config with 'subnet_mask' set.
    """
    results: list[dict[str, Any]] = []
    missing: list[str] = []

    for interface in static_interfaces:
        interface_name: str = interface["name"]
        expected_ip: str = interface["ip_address"]
        source_mac: str = interface.get("macAddress", "").lower().replace("-", ":").replace(".", ":")

        # Phase 1: exact MAC match
        matched = None
        if source_mac:
            for _iface_name, iface_config in current_interfaces.items():
                if iface_config.get("macAddress", "").lower().replace("-", ":").replace(".", ":") == source_mac:
                    matched = iface_config
                    break

        # Phase 2: exact name match
        if not matched:
            for iface_name, iface_config in current_interfaces.items():
                if interface_name.lower() == iface_name.lower():
                    matched = iface_config
                    break

        # Phase 3: unique substring name match
        if not matched:
            substring_matches = [
                cfg for name, cfg in current_interfaces.items() if interface_name.lower() in name.lower()
            ]
            if len(substring_matches) == 1:
                matched = substring_matches[0]

        if not matched:
            missing.append(f"{interface_name} (interface not found)")
            continue

        ip_found = False
        for ip_info in matched.get("ip_addresses", []):
            if ip_info.get("ip_address") == expected_ip:
                result = dict(matched)
                result["subnet_mask"] = ip_info.get("subnet_mask", "")
                results.append(result)
                ip_found = True
                break

        if not ip_found:
            all_ips = [ip.get("ip_address") for ip in matched.get("ip_addresses", [])]
            missing.append(f"{interface_name}/{expected_ip} (found IPs: {all_ips})")

    return results, missing


def check_static_ip_preservation(
    vm_name: str,
    vm_ssh_connections: SSHConnectionManager,
    source_vm_data: dict[str, Any],
    source_provider_data: dict[str, Any],
    network_map_resource: NetworkMap,
    destination_vm: dict[str, Any],
    timeout: int = 660,
    retry_delay: int = 30,
) -> None:
    """Verify that all static IPs from the source VM are preserved on the destination VM after migration.

    Connects to the destination VM via SSH and polls until every expected static IP appears in the
    guest network configuration, then validates subnet masks and gateways.

    Args:
        vm_name: Name of the VM to check
        vm_ssh_connections: SSH connections fixture manager
        source_vm_data: Source VM data collected during plan setup
        source_provider_data: Provider configuration from .providers.json
        network_map_resource: NetworkMap resolving source networks to destinations
        destination_vm: Destination VM data with network_interfaces
        timeout: Total timeout in seconds for network configuration retrieval (default: 660)
        retry_delay: Delay in seconds between retry attempts (default: 30)

    Raises:
        ValueError: If no static interfaces found or network command fails.
        TimeoutError: If not all static IPs appear within the timeout period.
        AssertionError: If subnet mask or gateway verification fails.
    """
    LOGGER.info(f"Verifying static IP preservation for VM {vm_name}")

    is_windows = source_vm_data.get("win_os", False)
    network_cmd = _WINDOWS_NETWORK_CMD if is_windows else ["nmcli", "device", "show"]

    # Extract static interfaces
    static_interfaces = _extract_static_interfaces(source_vm_data)

    if not static_interfaces:
        raise ValueError(
            f"preserve_static_ips is enabled but no static IP interfaces found for VM {vm_name}. "
            "Ensure the source VM is powered on so guest tools can report IP origin information."
        )

    # Interfaces the product intentionally does NOT preserve: pod-mapped (the pod
    # network has a different subnet) and non-preserve guest IP modes (ignored
    # destinations, networkIPMode dhcp/none — forklift MTV-5674).
    excluded_interfaces = [
        interface
        for interface in static_interfaces
        if _is_pod_mapped_static_interface(
            static_interface=interface,
            destination_vm=destination_vm,
            network_map_resource=network_map_resource,
        )
        or _resolve_interface_guest_ip_mode(network_map_resource, interface) != "preserve"
    ]
    if excluded_interfaces:
        LOGGER.info(
            f"Excluded from static IP verification (pod-mapped or non-preserve guest IP mode): "
            f"{[(interface['name'], interface['ip_address']) for interface in excluded_interfaces]}"
        )
        static_interfaces = [interface for interface in static_interfaces if interface not in excluded_interfaces]

    if not static_interfaces:
        LOGGER.info(f"All static IP interfaces for VM {vm_name} are excluded from preservation, nothing to verify")
        return

    LOGGER.info(f"Found {len(static_interfaces)} static IP interfaces to verify")

    # Get SSH credentials
    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_data)

    ssh_conn = vm_ssh_connections.create(vm_name=vm_name, username=ssh_username, password=ssh_password)
    last_missing: list[str] = []

    LOGGER.info(
        f"Verifying {len(static_interfaces)} static IPs: "
        f"{[(iface['name'], iface['ip_address']) for iface in static_interfaces]}"
    )

    def verify_all_static_ips() -> list[dict[str, Any]] | None:
        """Check all expected static IPs in a single SSH call.

        Returns:
            list[dict[str, Any]]: Matched interface configs (one per static_interfaces entry) when ALL found.
            None: If any IP is missing (triggers retry).

        Raises:
            CommandExecFailed: If the network command fails.
        """
        nonlocal last_missing
        try:
            with ssh_conn:
                current_interfaces = load_current_interfaces(
                    ssh_conn=ssh_conn,
                    network_cmd=network_cmd,
                    is_windows=is_windows,
                    vm_name=vm_name,
                )
            if current_interfaces is None:
                return None

            results, missing = _match_expected_static_ips(
                current_interfaces=current_interfaces,
                static_interfaces=static_interfaces,
            )

            if missing:
                last_missing = missing
                LOGGER.warning(f"Not all static IPs found yet, missing: {missing}")
                return None

            LOGGER.info(f"All {len(static_interfaces)} static IPs found")
            return results

        except CommandExecFailed:
            raise
        except (SSHException, ChannelException, NoValidConnectionsError, AuthenticationException) as e:
            LOGGER.warning(f"SSH connection failed: {e}")
            return None
        except Exception as e:
            LOGGER.error(f"Unexpected error during network config retrieval: {type(e).__name__}: {e}")
            raise

    try:
        matched_interfaces: list[dict[str, Any]] | None = None
        for sample in TimeoutSampler(wait_timeout=timeout, sleep=retry_delay, func=verify_all_static_ips):
            if sample:
                matched_interfaces = sample
                break
    except TimeoutExpiredError as e:
        raise TimeoutError(
            f"Static IP verification timed out after {timeout}s for VM {vm_name}. Last missing: {last_missing}"
        ) from e
    except CommandExecFailed as e:
        raise ValueError(f"Network configuration command failed: {' '.join(network_cmd)} - {e}") from e

    assert matched_interfaces is not None

    for interface, matched in zip(static_interfaces, matched_interfaces, strict=True):
        interface_name = interface["name"]
        expected_subnet = interface.get("subnet_mask", "")
        expected_gateway = interface.get("gateway", "")
        expected_dns_servers = interface.get("dns_servers", [])

        if expected_subnet:
            _verify_subnet_mask(interface_name, expected_subnet, matched)

        _verify_gateway(interface_name, expected_gateway, matched)

        _verify_dns_servers(interface_name, expected_dns_servers, matched, is_windows)

        LOGGER.info(f"Static IP {interface['ip_address']} verified for interface {interface_name}")

    LOGGER.info(f"Static IP preservation verification completed for VM {vm_name}")


def map_item_matches_source_network(map_item: Any, source_vm_network: str | None) -> bool:
    """Return True if a migration-map entry's source matches the given network.

    Matches the entry's source ``type``, ``name`` (stripping a ``namespace/name``
    prefix), or ``id``.

    Args:
        map_item (Any): Map entry from ``spec.map`` — an untyped dynamic-resource object.
        source_vm_network (str | None): Normalized source network identifier to match.

    Returns:
        True if this map entry's source refers to the given source network.
    """
    if map_item.source.type and map_item.source.type == source_vm_network:
        return True

    if map_item.source.name:
        name_to_compare = map_item.source.name.split("/")[1] if "/" in map_item.source.name else map_item.source.name
        if name_to_compare == source_vm_network:
            return True

    return bool(map_item.source.id and map_item.source.id == source_vm_network)


def _resolve_interface_guest_ip_mode(network_map_resource: NetworkMap, interface: dict[str, Any]) -> str:
    """Resolve the guest IP mode the NetworkMap applies to an interface.

    Mirrors Forklift's NIC mode resolution (MTV-5674): ignored destination forces
    "none"; entry-level networkIPMode overrides the plan-level preserveStaticIPs
    default; entries without networkIPMode keep "preserve".

    Args:
        network_map_resource (NetworkMap): NetworkMap resource resolving source networks to destinations.
        interface (dict[str, Any]): Interface mapping with a 'network' key (source NIC
            or flattened static-IP entry from _extract_static_interfaces).

    Returns:
        The resolved guest IP mode: "preserve", "dhcp" or "none".

    Raises:
        ValueError: If no NetworkMap entry matches the interface's source network.
    """
    source_vm_network = normalize_source_network(interface)
    for map_item in network_map_resource.instance.spec.map:
        if map_item_matches_source_network(map_item, source_vm_network):
            if map_item.destination.type == "ignored":
                return "none"
            return getattr(map_item, "networkIPMode", None) or "preserve"
    raise ValueError(
        f"No NetworkMap entry matches source network {source_vm_network!r} for interface '{interface.get('name')}'"
    )


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


def check_cpu_features(
    destination_vm: dict[str, Any],
    expected_features: list[dict[str, str]],
) -> None:
    """Verify CPU feature policies on the migrated VM.

    Args:
        destination_vm (dict[str, Any]): Destination VM data dictionary
        expected_features (list[dict[str, str]]): Expected CPU features with policies,
            e.g. [{"name": "vmx", "policy": "disable"}, {"name": "svm", "policy": "disable"}]

    Raises:
        AssertionError: If CPU features do not match expected configuration
    """
    actual_features: list[dict[str, Any]] = destination_vm["cpu"].get("features", [])
    for expected in expected_features:
        feature_name = expected["name"]
        expected_policy = expected["policy"]
        matching = [f for f in actual_features if f.get("name") == feature_name]
        assert matching, f"CPU feature '{feature_name}' not found on migrated VM. Actual features: {actual_features}"
        actual_policy = matching[0].get("policy")
        assert actual_policy is not None, (
            f"CPU feature '{feature_name}' found but has no 'policy' field. Feature data: {matching[0]}"
        )
        assert actual_policy == expected_policy, (
            f"CPU feature '{feature_name}' has policy '{actual_policy}', expected '{expected_policy}'"
        )

    LOGGER.info(f"CPU feature verification passed: {[f['name'] + '=' + f['policy'] for f in expected_features]}")


def check_vbs_status(
    vm_name: str,
    vm_ssh_connections: SSHConnectionManager,
    source_provider_data: dict[str, Any],
    source_vm_info: dict[str, Any],
) -> None:
    """Verify Virtualization Based Security (VBS) is not running on a Windows VM.

    Connects via SSH to the Windows guest and queries the DeviceGuard WMI class
    to confirm VirtualizationBasedSecurityStatus is not 2 (running).
    Status 1 (enabled but not running) is expected when nested virtualization
    extensions are disabled at the emulator level.

    Args:
        vm_name (str): Name of the VM to check
        vm_ssh_connections (SSHConnectionManager): SSH connections manager
        source_provider_data (dict[str, Any]): Source provider data with Windows credentials
        source_vm_info (dict[str, Any]): VM information including OS type

    Raises:
        ConnectionError: If SSH connection cannot be established to the VM
        ValueError: If credentials are not found or VBS status command fails
        AssertionError: If VBS is still running (status 2)
    """
    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_info)
    ssh_conn = vm_ssh_connections.create(vm_name=vm_name, username=ssh_username, password=ssh_password)

    vbs_cmd = [
        "powershell",
        "-Command",
        "(Get-CimInstance -Namespace root\\Microsoft\\Windows\\DeviceGuard "
        "-ClassName Win32_DeviceGuard).VirtualizationBasedSecurityStatus",
    ]

    with ssh_conn:
        if not ssh_conn.rrmngmnt_host:
            raise ConnectionError(f"SSH connection not established for VM {vm_name}")

        executor = ssh_conn.rrmngmnt_host.executor(user=ssh_conn.rrmngmnt_user)
        executor.port = ssh_conn.local_port
        rc, stdout, err = executor.run_cmd(vbs_cmd)

        if rc != 0:
            raise ValueError(f"VBS status command failed on VM '{vm_name}'. rc={rc}, stderr={err}")

        vbs_status = stdout.strip()
        assert vbs_status != _VBS_STATUS_RUNNING, (
            f"VBS is still running on VM '{vm_name}'. "
            f"VirtualizationBasedSecurityStatus: {vbs_status}, expected: not 2 (not running)"
        )
        LOGGER.info(f"VBS verification passed for VM '{vm_name}': VirtualizationBasedSecurityStatus={vbs_status}")


def check_memory(source_vm: dict[str, Any], destination_vm: dict[str, Any]) -> None:
    """Verify source and destination VM memory match.

    Args:
        source_vm: Source VM data dictionary.
        destination_vm: Destination VM data dictionary.

    Raises:
        ValueError: If memory values differ.
    """
    source_mem = source_vm["memory_in_mb"]
    dest_mem = destination_vm["memory_in_mb"]
    if source_mem != dest_mem:
        raise ValueError(f"Memory mismatch: source={source_mem}MB, destination={dest_mem}MB")


def _normalized_mac(mac_address: str) -> str:
    """Normalize a MAC address for comparison (lowercase, ':' and '-' separators stripped).

    Args:
        mac_address (str): MAC address in any common notation.

    Returns:
        str: The normalized MAC address.
    """
    return mac_address.replace(":", "").replace("-", "").lower()


def get_nic_by_mac(nics: list[dict[str, Any]], mac_address: str) -> dict[str, Any]:
    """Get a NIC from a list of NICs by MAC address (case- and separator-insensitive).

    Accepts provider-reported NICs and interfaces parsed from guest ipconfig output.

    Args:
        nics (list[dict[str, Any]]): List of NIC dictionaries with "macAddress" keys.
        mac_address (str): The MAC address to search for.

    Returns:
        dict[str, Any]: The matching NIC dictionary.

    Raises:
        ValueError: If no NIC matches the MAC address.
    """
    matched = [nic for nic in nics if _normalized_mac(nic.get("macAddress") or "") == _normalized_mac(mac_address)]
    if not matched:
        raise ValueError(
            f"No NIC found with MAC address '{mac_address}' in {len(nics)} NICs "
            f"(available: {[n.get('macAddress') for n in nics]})"
        )
    return matched[0]


def get_all_destinations(map_resource: NetworkMap, source_vm_nic: dict[str, Any]) -> list[str]:
    """Return every destination network name a source NIC's network maps to.

    A source network maps to multiple destination NADs when per-NIC network mapping is used
    (two NICs on the same source network each get their own NAD). Forklift's NADPool decides
    which NIC binds to which NAD at migration time, so a source NIC may land on any of them.
    Returns all candidates so the caller can assert the destination NIC's NAD is one of them.

    Args:
        map_resource: NetworkMap resource whose spec.map is inspected.
        source_vm_nic: Source VM NIC dict with a "network" name/id/dict.

    Returns:
        Destination network names (``"pod"`` for pod-type entries), one per matching map entry.
    """
    source_vm_network = normalize_source_network(source_vm_nic)
    destinations: list[str] = []
    for map_item in map_resource.instance.spec.map:
        if map_item_matches_source_network(map_item, source_vm_network):
            destinations.append("pod" if map_item.destination.type == "pod" else map_item.destination.name)

    return destinations


def check_network(source_vm: dict[str, Any], destination_vm: dict[str, Any], network_migration_map: NetworkMap) -> None:
    for source_vm_nic in source_vm["network_interfaces"]:
        # NICs whose source network maps to an ignored destination are
        # intentionally dropped from the destination VM — they must be absent.
        if any(
            map_item.destination.type == "ignored"
            for map_item in network_migration_map.instance.spec.map
            if map_item_matches_source_network(map_item, normalize_source_network(source_vm_nic))
        ):
            destination_macs = {_normalized_mac(nic["macAddress"]) for nic in destination_vm["network_interfaces"]}
            assert _normalized_mac(source_vm_nic["macAddress"]) not in destination_macs, (
                f"NIC (MAC {source_vm_nic['macAddress']}) on ignored source network "
                f"'{source_vm_nic['network']}' is still present on the destination VM"
            )
            continue

        expected_networks = get_all_destinations(network_migration_map, source_vm_nic)

        assert expected_networks, f"Network '{source_vm_nic['network']}' not found in migration map"

        destination_vm_nic = get_nic_by_mac(
            nics=destination_vm["network_interfaces"], mac_address=source_vm_nic["macAddress"]
        )

        # A source network may map to several destination NADs (per-NIC mapping); the destination
        # NIC must be attached to one of them.
        assert destination_vm_nic["network"] in expected_networks, (
            f"NIC (MAC {source_vm_nic['macAddress']}) on source network '{source_vm_nic['network']}' "
            f"landed on destination network '{destination_vm_nic['network']}', "
            f"expected one of {expected_networks}"
        )


def check_nic_name_preservation(
    source_vm_data: dict[str, Any],
    destination_vm: dict[str, Any],
    network_map_resource: NetworkMap,
) -> None:
    """Verify that guest OS NIC names are preserved after migration.

    Compares source NIC names (collected via VMware Guest Operations before migration)
    against destination NIC names (reported by guest agent on KubeVirt VMI).
    Matches NICs by MAC address. NICs on networks mapped with guest IP mode 'none'
    are skipped: the product applies no guest configuration to them, so they are not
    renamed to their source names.

    Args:
        source_vm_data: Source VM data from plan["source_vms_data"], containing
            network_interfaces with guest_nic_name field
        destination_vm: Destination VM data from vm_dict(), containing
            network_interfaces with guest_interface_name field
        network_map_resource: NetworkMap resolving source networks to destinations

    Raises:
        ValueError: If source or destination has no network interfaces
        AssertionError: If any NIC name doesn't match between source and destination
    """
    source_nics = source_vm_data.get("network_interfaces", [])
    dest_nics = destination_vm.get("network_interfaces", [])

    if not source_nics:
        raise ValueError("Source VM has no network interfaces — cannot verify NIC name preservation")
    if not dest_nics:
        raise ValueError("Destination VM has no network interfaces — cannot verify NIC name preservation")

    for source_nic in source_nics:
        source_nic_name = source_nic.get("guest_nic_name")
        if not source_nic_name:
            continue

        source_mac = source_nic.get("macAddress", "").lower()

        if _resolve_interface_guest_ip_mode(network_map_resource=network_map_resource, interface=source_nic) == "none":
            LOGGER.info(f"Skipping NIC name preservation for none-mode NIC {source_nic_name} (MAC {source_mac})")
            continue

        for dest_nic in dest_nics:
            if dest_nic.get("macAddress", "").lower() == source_mac:
                dest_nic_name = dest_nic.get("guest_interface_name", "")
                assert dest_nic_name == source_nic_name, (
                    f"NIC name mismatch: source {source_nic_name} != destination {dest_nic_name} (MAC {source_mac})"
                )
                LOGGER.info(f"NIC name preserved: {source_nic_name} (MAC {source_mac})")
                break


def _verify_kept_disks_attached(
    vm: dict[str, Any],  # untyped pytest-testconfig JSON; accessed fields documented below
    destination_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    vm_target_namespace: str,
) -> int:
    """Verify a migrated VM has exactly its non-excluded disks attached.

    Splits the source VM disks, read from the Forklift inventory, into excluded and kept
    sets using the Plan excludeDisks bus addresses, then compares the destination VM
    disks against the kept set by serial provenance and capacity: each kept source
    disk's serial (the vSphere disk UUID prefix forklift preserves as the destination
    disk serial) must appear on a destination disk, no destination disk may carry an
    excluded disk's serial, and capacities are verified per disk, paired by serial —
    Block volumes require an exact match (aligned to 1 MiB), Filesystem volumes are
    checked within CDI-overhead bounds because CDI inflates their PVC requests.
    Serial provenance distinguishes same-sized kept and excluded disks that capacities
    alone cannot tell apart.

    Args:
        vm (dict[str, Any]): VM configuration dict from prepared_plan["virtual_machines"]
            containing the "exclude_disks" bus address list.
        destination_provider (BaseProvider): Destination provider of the migrated VM.
        source_provider_inventory (ForkliftInventory): Source provider inventory.
        vm_target_namespace (str): Namespace of the migrated VM.

    Returns:
        int: Number of non-excluded (kept) source disks.

    Raises:
        AssertionError: If the destination VM disks do not match the non-excluded source
            disks by serial, or a kept disk's destination capacity does not satisfy its
            volume mode's expectation.
        ValueError: If the excludeDisks bus address list contains duplicates, an
            excludeDisks bus address matches no source disk, the inventory disk list
            for the VM is missing or malformed, a source disk reports a busAddress
            or serial that is not a non-empty string, serial-prefix ambiguity exists
            among the VM's disks (kept or excluded disks sharing a 20-character
            serial prefix), or the destination disks share duplicate serials.
    """
    excluded_addresses = vm.get("exclude_disks", [])
    vm_name = vm["name"]

    # Inventory validation
    if len(excluded_addresses) != len(set(excluded_addresses)):
        raise ValueError(f"Duplicate bus addresses in exclude_disks for VM '{vm_name}': {excluded_addresses}")

    inventory_vm = source_provider_inventory.get_vm(name=vm_name)
    inventory_disks = inventory_vm.get("disks")
    if not isinstance(inventory_disks, list) or not inventory_disks:
        raise ValueError(f"Forklift inventory reported no valid disk list for VM '{vm_name}'")

    source_disks: list[dict[str, Any]] = inventory_disks
    for disk in source_disks:
        if not isinstance(disk, dict):
            raise ValueError(f"Malformed disk entry in Forklift inventory for VM '{vm_name}': {disk}")
        for field in ("busAddress", "serial"):
            # String type is required: non-string values would reach set comparisons and serial slicing
            field_value = disk.get(field)
            if not isinstance(field_value, str) or not field_value:
                raise ValueError(
                    f"Malformed disk entry in Forklift inventory for VM '{vm_name}': "
                    f"'{field}' must be a non-empty string, got {field_value!r}"
                )

    # Kept/excluded partitioning
    excluded_disks = [disk for disk in source_disks if disk.get("busAddress") in excluded_addresses]
    unmatched_addresses = set(excluded_addresses) - {disk.get("busAddress") for disk in excluded_disks}
    if unmatched_addresses:
        raise ValueError(
            f"excludeDisks bus addresses {sorted(unmatched_addresses)} match no disk on source VM '{vm_name}'"
        )

    kept_disks = [disk for disk in source_disks if disk.get("busAddress") not in excluded_addresses]

    # Serial verification
    destination_vm = destination_provider.vm_dict(
        name=resolve_destination_vm_name(vm),
        namespace=vm_target_namespace,
    )

    # Ambiguity guard: set comparisons cannot tell disks apart when serial prefixes collide
    kept_serial_prefixes = [disk["serial"][:_DISK_SERIAL_MAX] for disk in kept_disks]
    excluded_serial_prefixes = [disk["serial"][:_DISK_SERIAL_MAX] for disk in excluded_disks]
    if len(set(kept_serial_prefixes)) != len(kept_serial_prefixes) or set(kept_serial_prefixes) & set(
        excluded_serial_prefixes
    ):
        raise ValueError(
            f"VM '{vm_name}': kept and/or excluded disks share a 20-character serial prefix — "
            f"disk identity cannot be established (kept: {kept_serial_prefixes}, excluded: {excluded_serial_prefixes})"
        )
    destination_disk_serials = [disk["serial"] for disk in destination_vm["disks"]]
    if len(set(destination_disk_serials)) != len(destination_disk_serials):
        raise ValueError(
            f"VM '{vm_name}': destination disks share serials — disk identity cannot be established: {destination_disk_serials}"
        )

    kept_serials = {disk["serial"][:_DISK_SERIAL_MAX] for disk in kept_disks}
    destination_serials = {disk["serial"] for disk in destination_vm["disks"]}
    assert kept_serials == destination_serials, (
        f"VM '{vm_name}': destination disk serials {destination_serials} must exactly match the kept source "
        f"disks' serial prefixes {kept_serials} (excluded bus addresses: {excluded_addresses}) — this catches "
        "a same-sized excluded disk being migrated instead of a kept one"
    )

    # Capacity verification
    LOGGER.info(
        f"VM '{vm_name}': excluded bus addresses {excluded_addresses}, "
        f"kept source disk serial prefixes {kept_serials}; verifying per-disk serial-paired capacities"
    )
    destination_disks_by_serial = {disk["serial"]: disk for disk in destination_vm["disks"]}
    for disk in kept_disks:
        destination_disk = destination_disks_by_serial[disk["serial"][:_DISK_SERIAL_MAX]]
        # Forklift aligns DataVolume capacity up to 1 MiB blocks when sizing destination PVCs.
        capacity = int(disk["capacity"])
        align = _DATAVOLUME_ALIGN_BLOCK_BYTES
        expected_capacity_kb = (capacity + align - 1) // align * align // 1024
        actual_capacity_kb = destination_disk["size_in_kb"]
        if destination_disk["volume_mode"] == "Block":
            assert actual_capacity_kb == expected_capacity_kb, (
                f"VM '{vm_name}': destination disk with serial '{destination_disk['serial']}' (volumeMode "
                f"Block) has capacity {actual_capacity_kb} KB, expected exactly {expected_capacity_kb} KB "
                f"(source capacity aligned to 1 MiB)"
            )
        else:
            # CDI inflates Filesystem-mode PVC requests by filesystem overhead; only bounds are enforceable
            max_capacity_kb = expected_capacity_kb / (1 - _CDI_FILESYSTEM_OVERHEAD) + _PVC_REQUEST_ROUNDING_SLACK_KB
            assert expected_capacity_kb <= actual_capacity_kb <= max_capacity_kb, (
                f"VM '{vm_name}': destination disk with serial '{destination_disk['serial']}' (volumeMode "
                f"Filesystem) has capacity {actual_capacity_kb} KB, expected between {expected_capacity_kb} "
                f"KB and {int(max_capacity_kb)} KB (CDI filesystem overhead bounds)"
            )
    return len(kept_disks)


def verify_excluded_disks(
    plan: dict[str, Any],  # untyped pytest-testconfig JSON; accessed fields documented below
    ocp_admin_client: DynamicClient,
    destination_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
) -> None:
    """Verify that disks excluded from migration are absent from the migration outcome.

    For every VM in the plan, verifies the migrated VM has exactly its non-excluded
    disks attached, identified by disk serial provenance (each kept source disk's
    serial prefix must appear on a destination disk; the excluded disks' serials must
    not) and by per-disk capacity paired by serial (exact for Block volumes, within
    CDI-overhead bounds for Filesystem volumes). Then verifies the VM namespace holds
    exactly one PVC per non-excluded disk across all plan VMs, so no volume was created
    for an excluded disk.

    The namespace check requires the plan's VMs to run in a dedicated namespace (plan
    config vm_target_namespace); a shared namespace would contain PVCs of other VMs.

    Args:
        plan (dict[str, Any]): Prepared plan whose virtual_machines each contain the
            "exclude_disks" bus address list, with _vm_target_namespace set.
        ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
        destination_provider (BaseProvider): Destination provider of the migrated VMs.
        source_provider_inventory (ForkliftInventory): Source provider inventory.

    Returns:
        None.

    Raises:
        AssertionError: If a migrated VM's disks or the namespace PVC set do not match
            the non-excluded source disks.
        ValueError: If an excludeDisks bus address matches no source disk, the inventory
            disk list for a VM is missing or malformed, or a source disk has no serial
            in the inventory.
    """
    vm_target_namespace = plan["_vm_target_namespace"]
    expected_namespace_pvc_count = 0

    for vm in plan["virtual_machines"]:
        expected_namespace_pvc_count += _verify_kept_disks_attached(
            vm=vm,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            vm_target_namespace=vm_target_namespace,
        )

    namespace_pvcs = list(PersistentVolumeClaim.get(client=ocp_admin_client, namespace=vm_target_namespace))
    assert len(namespace_pvcs) == expected_namespace_pvc_count, (
        f"Namespace '{vm_target_namespace}' holds {len(namespace_pvcs)} PVC(s), expected "
        f"{expected_namespace_pvc_count} (one per non-excluded disk of the plan's VMs)"
    )


def check_storage(
    source_vm: dict[str, Any],
    destination_vm: dict[str, Any],
    storage_map_resource: StorageMap,
    # Override the destination disk-count expectation when the migrated disk set intentionally differs
    # from the source (e.g. disk exclusion: excluded disks are neither copied nor attached)
    expected_disk_count: int | None = None,
) -> None:
    """Verify destination VM disks match the source and the storage map.

    Args:
        source_vm (dict[str, Any]): Source VM info dictionary.
        destination_vm (dict[str, Any]): Destination VM info dictionary.
        storage_map_resource (StorageMap): StorageMap resource used for the migration.
        expected_disk_count (int | None): Expected number of disks on the destination
            VM. Defaults to the source VM disk count; override when the migrated disk
            set intentionally differs from the source (e.g. disk exclusion: excluded
            disks are neither copied nor attached).

    Raises:
        AssertionError: If the disk count or a storage class does not match.
    """
    destination_disks = destination_vm["disks"]
    source_vm_disks_storage = [disk["storage"]["name"] for disk in source_vm["disks"]]

    if expected_disk_count is None:
        expected_disk_count = len(source_vm["disks"])
    assert len(destination_disks) == expected_disk_count, "disks count"

    for destination_disk in destination_disks:
        assert destination_disk["storage"]["name"] == py_config["storage_class"], "storage class"
        if destination_disk["storage"]["name"] == "ocs-storagecluster-ceph-rbd":
            for mapping in storage_map_resource.instance.spec.map:
                if mapping.source.name in source_vm_disks_storage:
                    # The following condition is for a customer case (BZ#2064936)
                    if mapping.destination.get("accessMode") == "ReadWriteOnce":
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

    uses_file_name = re.search(r"\{\{-?\s*\.FileName\b", pvc_name_template) is not None

    # Validate VMware-only wildcard (requires VMDK filenames from vSphere inventory)
    if uses_file_name:
        if not source_provider or source_provider.type != Provider.ProviderType.VSPHERE:
            LOGGER.warning(
                "{{.FileName}} wildcard in pvcNameTemplate is only supported for VMware/vSphere provider. "
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
        if uses_file_name and not filename:
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


def check_boot_configuration(source_vm: dict[str, Any], destination_vm: dict[str, Any]) -> None:
    """Verify boot configuration is preserved after migration.

    Args:
        source_vm: Source VM info dictionary with firmware data.
        destination_vm: Destination VM info dictionary with firmware data.

    Raises:
        AssertionError: If firmware type, secure boot, or TPM settings don't match.
        KeyError: If required firmware keys are missing in source or destination VM info.
    """
    source_firmware: dict[str, Any] = source_vm["firmware"]
    dest_firmware: dict[str, Any] = destination_vm["firmware"]

    firmware_checks: list[tuple[str, str]] = [
        ("boot_firmware", "Boot firmware"),
        ("secure_boot", "Secure boot"),
        ("tpm_present", "TPM"),
    ]
    mismatches: list[str] = []
    for key, label in firmware_checks:
        if source_firmware[key] != dest_firmware[key]:
            mismatches.append(f"{label}: source={source_firmware[key]}, destination={dest_firmware[key]}")
    if mismatches:
        raise AssertionError(f"Firmware mismatch for VM '{destination_vm['name']}': {'; '.join(mismatches)}")

    LOGGER.info(
        f"Firmware checks passed for VM '{destination_vm['name']}': "
        f"boot={dest_firmware['boot_firmware']}, secure_boot={dest_firmware['secure_boot']}, "
        f"tpm={dest_firmware['tpm_present']}"
    )


def check_disconnected_nic_state(destination_vm: dict[str, Any], expected_mac: str) -> None:
    """Verify a NIC with StartConnected=False on the source has state=down on the migrated OCP VM.

    Reads from the VM spec (not VMI status) because KubeVirt does not report
    link-down interfaces in VMI status.interfaces.

    Args:
        destination_vm: Destination VM info dictionary containing provider_vm_api.
        expected_mac: MAC address (lowercase) of the NIC expected to have state=down.

    Raises:
        AssertionError: If no interface with expected_mac is found, or its state is not 'down'.
        ValueError: If destination_vm has no provider_vm_api.
    """
    cnv_vm = destination_vm.get("provider_vm_api")
    if cnv_vm is None:
        raise ValueError(f"destination_vm '{destination_vm.get('name')}' has no provider_vm_api")

    interfaces: list[dict[str, Any]] = cnv_vm.instance.spec.template.spec.domain.devices.interfaces
    matching = [iface for iface in interfaces if iface.get("macAddress", "").lower() == expected_mac]

    assert matching, (
        f"No interface with MAC '{expected_mac}' found on VM '{destination_vm['name']}'. "
        f"Interfaces: {[iface.get('macAddress') for iface in interfaces]}"
    )

    state = matching[0].get("state")
    assert state == "down", (
        f"Interface with MAC '{expected_mac}' on VM '{destination_vm['name']}' has state={state!r}, expected 'down'"
    )
    LOGGER.info(f"Disconnected NIC state verified: MAC={expected_mac}, state=down on VM '{destination_vm['name']}'")


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


def get_vm_command_output(
    vm: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_vm_info: dict[str, Any],
    source_provider_data: dict[str, Any],
    command: list[str],
) -> str:
    """Execute a command on a migrated VM via SSH and return its stdout.

    Args:
        vm: VM dict from plan["virtual_machines"]
        vm_ssh_connections: SSH connections fixture manager
        source_vm_info: Source VM information from source_provider.vm_dict()
        source_provider_data: Provider configuration from .providers.json
        command: Command to execute

    Returns:
        Command stdout.

    Raises:
        ValueError: If VM is Windows (Linux-only feature)
    """
    vm_name = vm["name"]
    destination_vm_name = resolve_destination_vm_name(vm)

    if source_vm_info.get("win_os", False):
        raise ValueError(
            f"Command output verification is only supported on Linux guests. VM '{vm_name}' is a Windows guest."
        )

    LOGGER.info(f"Running command on VM {destination_vm_name}: {' '.join(command)}")

    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_info)
    ssh_conn = vm_ssh_connections.create(vm_name=destination_vm_name, username=ssh_username, password=ssh_password)

    with ssh_conn:
        stdout = run_cmd_in_vm(ssh_conn, command, f"Command on VM {destination_vm_name}")
        LOGGER.info(f"Command output:\n{stdout}")
        return stdout


def check_vm_command_output(
    vm: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
    source_vm_info: dict[str, Any],
    source_provider_data: dict[str, Any],
    command: list[str],
    expected_output: str,
) -> None:
    """Execute a command on VM via SSH and verify expected output is present.

    Args:
        vm: VM dict from plan["virtual_machines"]
        vm_ssh_connections: SSH connections fixture manager
        source_vm_info: Source VM information from source_provider.vm_dict()
        source_provider_data: Provider configuration from .providers.json
        command: Command to execute
        expected_output: Expected string in command output

    Raises:
        ValueError: If VM is Windows (Linux-only feature)
        AssertionError: If expected_output not found in command stdout
    """
    destination_vm_name = resolve_destination_vm_name(vm)
    stdout = get_vm_command_output(
        vm=vm,
        vm_ssh_connections=vm_ssh_connections,
        source_vm_info=source_vm_info,
        source_provider_data=source_provider_data,
        command=command,
    )

    if expected_output not in stdout:
        raise AssertionError(
            f"Expected '{expected_output}' not found in command output on VM {destination_vm_name}. "
            f"Command: {' '.join(command)}\n"
            f"Output: {stdout}"
        )


def _verify_warm_vsphere_di(
    plan_resource: Plan,
    plan: dict[str, Any],
    di_results: list[dict[str, Any]] | None,
) -> None:
    """Verify Deep Inspection results for a warm vSphere migration.

    Args:
        plan_resource (Plan): The Plan CR resource.
        plan (dict[str, Any]): The prepared plan configuration.
        di_results (list[dict[str, Any]] | None): Captured DI data, if any.

    Raises:
        ValueError: If DI validation fails or expected results are missing.
    """
    if plan.get("run_preflight_inspection") is False:
        if di_results:
            raise ValueError(
                f"Plan '{plan_resource.name}': DI capture callback recorded results for "
                f"{len(di_results)} VM(s) despite run_preflight_inspection=False. "
                f"VMs: {[r['vm_name'] for r in di_results]}"
            )
        LOGGER.info(f"Plan '{plan_resource.name}': No DI results captured (DI correctly skipped).")
        return

    if di_results is None:
        raise ValueError(
            f"Plan '{plan_resource.name}': DI results not captured. "
            f"Pass di_results from create_di_capture_callback() to check_vms()."
        )
    expected_vm_names = {vm["name"] for vm in plan["virtual_machines"]}
    verify_captured_di_results(di_results=di_results, plan_name=plan_resource.name, expected_vm_names=expected_vm_names)


def verify_rdm_disk_bus_types(
    destination_provider: OCPProvider,
    plan: dict[str, Any],
) -> None:
    """Verify that migrated RDM disks have LUN device type with SCSI bus.

    When rdmAsLun=true is set on the migration plan, RDM disks should be mapped as
    KubeVirt LUN devices (lun.bus: scsi) instead of regular disk devices (disk.bus: virtio).

    Args:
        destination_provider: OCP destination provider instance.
        plan: Test plan dictionary containing VM configuration.

    Raises:
        AssertionError: If disk bus types don't match expected configuration.
        ValueError: If destination_provider.ocp_resource is not set.
    """
    if not destination_provider.ocp_resource:
        raise ValueError("destination_provider.ocp_resource is not set")

    for vm_config in plan["virtual_machines"]:
        vm_name = vm_config["name"]
        dest_vm_name = resolve_destination_vm_name(vm_config)
        LOGGER.info(f"Verifying RDM disk bus types for migrated VM '{vm_name}'")

        migrated_vm = VirtualMachine(
            client=destination_provider.ocp_resource.client,
            name=dest_vm_name,
            namespace=plan["_vm_target_namespace"],
            ensure_exists=True,
        )
        disks: list[dict[str, Any]] = migrated_vm.instance.spec.template.spec.domain.devices.disks

        lun_disks = [d for d in disks if d.get("lun")]
        regular_disks = [d for d in disks if d.get("disk")]

        assert lun_disks, (
            f"No LUN-type disks found on VM '{vm_name}'. Expected at least one RDM disk mapped as LUN. Disks: {disks}"
        )

        for lun_disk in lun_disks:
            bus = lun_disk["lun"].get("bus")
            assert bus == "scsi", (
                f"LUN disk '{lun_disk.get('name')}' on VM '{vm_name}' has bus '{bus}', expected 'scsi'"
            )

        for reg_disk in regular_disks:
            bus = reg_disk["disk"].get("bus")
            assert bus == "virtio", (
                f"Regular disk '{reg_disk.get('name')}' on VM '{vm_name}' has bus '{bus}', expected 'virtio'"
            )

        LOGGER.info(
            f"VM '{vm_name}': {len(lun_disks)} LUN disk(s) with SCSI bus, "
            f"{len(regular_disks)} regular disk(s) with virtio bus — verified"
        )


def is_multus_network_name_always_qualified(client: "DynamicClient") -> bool:
    """Return whether ForkliftController forces qualified Multus networkName values.

    Reads (and caches) the ``controller_multus_network_name_always_qualified`` flag;
    tests never modify it.

    Args:
        client: DynamicClient connected to the cluster running MTV.

    Returns:
        True only when the flag is set to "true".

    Raises:
        ValueError: If the ForkliftController resource is not found.
    """
    global _multus_network_name_always_qualified_cache
    if _multus_network_name_always_qualified_cache is None:
        forklift_controller = ForkliftController(
            client=client,
            name=_FORKLIFT_CONTROLLER_NAME,
            namespace=py_config["mtv_namespace"],
        )
        try:
            controller_instance = forklift_controller.instance
        except NotFoundError as err:
            raise ValueError(
                f"ForkliftController '{_FORKLIFT_CONTROLLER_NAME}' not found in namespace '{py_config['mtv_namespace']}'"
            ) from err
        always_qualified_value = getattr(
            controller_instance.spec, "controller_multus_network_name_always_qualified", None
        )
        _multus_network_name_always_qualified_cache = str(always_qualified_value).lower() == "true"
    return _multus_network_name_always_qualified_cache


def check_vm_multus_network_name_form(
    destination_vm: dict[str, Any],
    network_map_resource: NetworkMap,
    vm_namespace: str,
) -> None:
    """Verify the form of Multus networkName values in the migrated VM spec.

    Each networkName must be the bare NAD name when the NAD is in the VM's namespace,
    ``<namespace>/<name>`` otherwise, or always when the controller flag is set.
    NIC-to-NAD identity is check_network's job.

    Args:
        destination_vm: Destination VM info dictionary containing provider_vm_api.
        network_map_resource: NetworkMap whose multus destinations define the expectation.
        vm_namespace: Namespace of the migrated VM.

    Raises:
        AssertionError: If a networkName is not a correctly-spelled mapped destination.
        ValueError: If the ForkliftController resource is not found.
    """
    mapped_multus_nads: set[tuple[str, str]] = {
        (map_item.destination.namespace or vm_namespace, map_item.destination.name)
        for map_item in network_map_resource.instance.spec.map
        if map_item.destination.type == "multus"
    }
    if not mapped_multus_nads:
        return

    always_qualified = is_multus_network_name_always_qualified(client=destination_vm["provider_vm_api"].client)
    expected_forms = {
        f"{namespace}/{name}" if namespace != vm_namespace or always_qualified else name
        for namespace, name in mapped_multus_nads
    }
    actual_forms = {
        network["multus"]["networkName"]
        for network in destination_vm["provider_vm_api"].instance.spec.template.spec.networks or []
        if network.get("multus")
    }

    assert actual_forms <= expected_forms, (
        f"VM '{destination_vm['name']}': unexpected Multus networkName forms "
        f"{sorted(actual_forms - expected_forms)}, allowed forms {sorted(expected_forms)}"
    )


def check_vms(
    plan: dict[str, Any],
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    network_map_resource: NetworkMap,
    storage_map_resource: StorageMap,
    source_provider_data: dict[str, Any],
    # Source namespaces apply only to OpenShift; other providers ignore None.
    source_vms_namespace: str | None,
    source_provider_inventory: ForkliftInventory | None = None,
    vm_ssh_connections: SSHConnectionManager | None = None,
    labeled_worker_node: dict[str, Any] | None = None,
    target_vm_labels: dict[str, Any] | None = None,
    plan_resource: Plan | None = None,
    di_results: list[dict[str, Any]] | None = None,
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
        destination_vm_name = resolve_destination_vm_name(vm)
        res[vm_name] = []

        source_vm_data: dict[str, Any] = plan.get("source_vms_data", {}).get(vm["name"], {})
        source_vm_kwargs: dict[str, Any] = {
            "name": vm_name,
            "namespace": source_vms_namespace,
            "source": True,
            "source_provider_inventory": source_provider_inventory,
        }
        if source_provider.type == Provider.ProviderType.HYPERV and source_vm_data:
            source_vm_kwargs["win_os"] = source_vm_data["win_os"]
        source_vm = source_provider.vm_dict(**source_vm_kwargs)
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

        # OpenStack and OCP-source adapters always write the qualified "<namespace>/<name>" form
        if source_provider.type not in (Provider.ProviderType.OPENSTACK, Provider.ProviderType.OPENSHIFT):
            try:
                check_vm_multus_network_name_form(
                    destination_vm=destination_vm,
                    network_map_resource=network_map_resource,
                    vm_namespace=vm_namespace,
                )
            except (AssertionError, ValueError) as exp:
                res[vm_name].append(f"check_vm_multus_network_name_form - {str(exp)}")

        if vm_guest_agent:
            try:
                check_guest_agent(destination_vm=destination_vm)
            except Exception as exp:
                res[vm_name].append(f"check_guest_agent - {str(exp)}")

        # SSH connectivity check - only when destination VM is powered on
        if vm_ssh_connections is not None and destination_vm.get("power_state") == "on":
            try:
                check_ssh_connectivity(
                    vm_name=destination_vm_name,
                    vm_ssh_connections=vm_ssh_connections,
                    source_provider_data=source_provider_data,
                    source_vm_info=source_vm,
                )
            except Exception as exp:
                res[vm_name].append(f"check_ssh_connectivity - {str(exp)}")

            # Static IP preservation check - for VMs with preserve_static_ips enabled, migrated from a
            # provider in _STATIC_IP_PROVIDERS

            # Fail fast: if preserve_static_ips is requested, source_vms_data must exist
            if plan.get("preserve_static_ips") and source_provider.type in _STATIC_IP_PROVIDERS:
                if not source_vm_data:
                    raise ValueError(
                        f"preserve_static_ips is enabled but source_vms_data is missing for VM '{vm['name']}'. "
                        "Ensure the prepared_plan fixture populates source_vms_data for static IP verification."
                    )

            if source_vm_data and plan.get("preserve_static_ips") and source_provider.type in _STATIC_IP_PROVIDERS:
                try:
                    check_static_ip_preservation(
                        vm_name=destination_vm_name,
                        vm_ssh_connections=vm_ssh_connections,
                        source_vm_data=source_vm_data,
                        source_provider_data=source_provider_data,
                        network_map_resource=network_map_resource,
                        destination_vm=destination_vm,
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_static_ip_preservation - {str(exp)}")

            # NIC name preservation check - only when preserve_static_ips is set
            # (udev rules are generated by the same firstboot script as static IP preservation)
            if (
                source_vm_data
                and plan.get("preserve_static_ips")
                and source_provider.type == Provider.ProviderType.VSPHERE
            ):
                try:
                    check_nic_name_preservation(
                        source_vm_data=source_vm_data,
                        destination_vm=destination_vm,
                        network_map_resource=network_map_resource,
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_nic_name_preservation - {str(exp)}")
        elif vm_ssh_connections is not None:
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

        # Nested virtualization checks not applicable for OCP→OCP migrations
        if (
            plan.get("enable_nested_virtualization") is False
            and source_provider.type != Provider.ProviderType.OPENSHIFT
        ):
            try:
                check_cpu_features(
                    destination_vm=destination_vm,
                    expected_features=_NESTED_VIRT_DISABLED_FEATURES,
                )
            except Exception as exp:
                res[vm_name].append(f"check_cpu_features - {str(exp)}")

            if (
                vm_ssh_connections is not None
                and destination_vm.get("power_state") == "on"
                and source_vm.get("win_os", False)
            ):
                try:
                    check_vbs_status(
                        vm_name=destination_vm_name,
                        vm_ssh_connections=vm_ssh_connections,
                        source_provider_data=source_provider_data,
                        source_vm_info=source_vm,
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_vbs_status - {str(exp)}")

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
                # Excluded disks are neither copied nor attached, so the destination
                # count is the source count minus distinct exclusions; an omitted
                # exclude_disks means an empty list (the VM keeps its full disk set)
                check_storage(
                    source_vm=source_vm,
                    destination_vm=destination_vm,
                    storage_map_resource=storage_map_resource,
                    expected_disk_count=len(source_vm["disks"]) - len(set(vm.get("exclude_disks", []))),
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

            try:
                check_boot_configuration(source_vm=source_vm, destination_vm=destination_vm)
            except Exception as exp:
                res[vm_name].append(f"check_boot_configuration - {str(exp)}")

            if disconnected_nic_mac := vm.get("disconnected_nic_mac"):
                try:
                    check_disconnected_nic_state(
                        destination_vm=destination_vm,
                        expected_mac=disconnected_nic_mac,
                    )
                except (AssertionError, ValueError) as exp:
                    res[vm_name].append(f"check_disconnected_nic_state - {str(exp)}")

        # Group 4: RHV-specific checks
        if rhv_provider(source_provider_data) and isinstance(source_provider, OvirtProvider):
            try:
                check_false_vm_power_off(source_provider=source_provider, source_vm=source_vm)
            except Exception as exp:
                res[vm_name].append(f"check_false_vm_power_off - {str(exp)}")

    # Group 5: Plan-driven DI verification (warm vSphere)
    if (
        plan_resource is not None
        and plan.get("warm_migration")
        and source_provider.type == Provider.ProviderType.VSPHERE
    ):
        try:
            _verify_warm_vsphere_di(plan_resource=plan_resource, plan=plan, di_results=di_results)
        except ValueError as exp:
            res.setdefault(plan_resource.name, []).append(f"warm_vsphere_di - {exp!s}")

    failed_checks = {vm_name: errors for vm_name, errors in res.items() if errors}
    if failed_checks:
        failure_details = "; ".join(f"{vm_name}: [{', '.join(errors)}]" for vm_name, errors in failed_checks.items())
        pytest.fail(f"VM validation failed — {failure_details}")
