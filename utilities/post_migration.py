from __future__ import annotations

import ipaddress
from typing import Any

import jc
import pytest
from ocp_resources.datavolume import DataVolume
from ocp_resources.network_map import NetworkMap
from ocp_resources.provider import Provider
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
from utilities.utils import rhv_provider

LOGGER = get_logger(name=__name__)


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
    vm_config: dict[str, Any],
    source_provider_data: dict[str, Any],
    source_vm_info: dict[str, Any],
) -> None:
    """
    Test SSH connectivity to a migrated VM using provider credentials.

    Args:
        vm_name: Name of the VM to test
        vm_ssh_connections: SSH connections fixture manager
        vm_config: VM configuration from the plan
        source_provider_data: Provider configuration from .providers.json
        source_vm_info: VM information including OS type

    Raises:
        Exception: If SSH connection cannot be established
    """
    LOGGER.info(f"Testing SSH connectivity to VM {vm_name}")

    # Get credentials from provider config
    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_info)

    # Create SSH connection
    ssh_conn = vm_ssh_connections.create(vm_name=vm_name, username=ssh_username, password=ssh_password)

    # Test SSH connectivity using rrmngmnt's built-in connectivity check
    with ssh_conn:
        if not ssh_conn.is_connective(tcp_timeout=10):
            raise ConnectionError("SSH connectivity test failed: Host is not connective")

        LOGGER.info(f"SSH connectivity to VM {vm_name} verified successfully")


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
        subnet_mask = ""

        for ipv4 in adapter.get("ipv4_addresses", []):
            ip_addresses.append({
                "ip_address": ipv4.get("address", ""),
                "status": ipv4.get("status", ""),
            })
            # Use first subnet mask found
            if not subnet_mask and ipv4.get("subnet_mask"):
                subnet_mask = ipv4.get("subnet_mask", "")

        interface_config: dict[str, Any] = {
            "name": interface_name,
            "ip_addresses": ip_addresses,
            "subnet_mask": subnet_mask,
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
        return

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

    if source_vm_data.get("win_os"):
        ipconfig_cmd = ["ipconfig", "/all"]
    else:
        raise NotImplementedError("The static IP verification is not implemented for non Windows OS")

    # Extract static interfaces
    static_interfaces = _extract_static_interfaces(source_vm_data)

    if not static_interfaces:
        LOGGER.info(f"No static IP interfaces found for VM {vm_name} - skipping verification")
        return

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

        def get_matching_interface_with_ip():
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
                    LOGGER.info(f"Attempting to get network configuration using ipconfig {' '.join(ipconfig_cmd)}")

                    # Execute command using executor with the correct user
                    # We need to use the executor with our specific user instead
                    executor = ssh_conn.rrmngmnt_host.executor(user=ssh_conn.rrmngmnt_user)
                    executor.port = ssh_conn.local_port

                    # Run the command directly
                    rc, stdout, err = executor.run_cmd(ipconfig_cmd)
                    if rc != 0:
                        raise CommandExecFailed(name=" ".join(ipconfig_cmd), err=err)

                    if not stdout.strip():
                        LOGGER.warning(f"{' '.join(ipconfig_cmd)} returned empty output")
                        return None

                    # Parse network configuration
                    try:
                        current_interfaces = _parse_windows_network_config(stdout)
                        LOGGER.info(f"{' '.join(ipconfig_cmd)} command executed successfully")
                    except Exception as e:
                        LOGGER.warning(f"Failed to parse {' '.join(ipconfig_cmd)} output: {e}")
                        return None

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

                    # Check if expected IP is found
                    interface_ips = matching_interface.get("ip_addresses", [])
                    all_ips = [ip_info.get("ip_address") for ip_info in interface_ips]

                    found_expected_ip = any(ip_info.get("ip_address") == expected_ip for ip_info in interface_ips)

                    if found_expected_ip:
                        LOGGER.info(f"SUCCESS: Expected IP {expected_ip} found")
                        return matching_interface
                    else:
                        LOGGER.warning(f"Expected IP {expected_ip} not found yet. Found IPs: {all_ips}")
                        return None

            except CommandExecFailed as e:
                # Permanent failure - don't retry
                LOGGER.warning(f"{' '.join(ipconfig_cmd)} command failed: {e}")
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
            raise RuntimeError(f"{' '.join(ipconfig_cmd)} command failed: {e}") from e

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
        pytest.fail(f"CPU failed checks: {failed_checks}")


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


def check_vms_power_state(
    source_vm: dict[str, Any], destination_vm: dict[str, Any], source_power_before_migration: bool
) -> None:
    assert source_vm["power_state"] == "off", "Checking source VM is off"

    if source_power_before_migration:
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
        pytest.fail(f"Some of the VM snapshots did not match: {failed_snapshots}")


def check_vms(
    plan: dict[str, Any],
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    destination_namespace: str,
    network_map_resource: NetworkMap,
    storage_map_resource: StorageMap,
    source_provider_data: dict[str, Any],
    source_vms_namespace: str,
    source_provider_inventory: ForkliftInventory | None = None,
    vm_ssh_connections: SSHConnectionManager | None = None,
) -> None:
    res: dict[str, list[str]] = {}
    should_fail: bool = False

    if source_provider.type == Provider.ProviderType.OVA:
        LOGGER.info("Source OVA VMS do not have any stats")
        return

    for vm in plan["virtual_machines"]:
        vm_name = vm["name"]
        res[vm_name] = []

        source_vm = source_provider.vm_dict(
            name=vm_name,
            namespace=source_vms_namespace,
            source=True,
            source_provider_inventory=source_provider_inventory,
        )
        vm_guest_agent = vm.get("guest_agent")
        destination_vm = destination_provider.vm_dict(
            wait_for_guest_agent=vm_guest_agent, name=vm_name, namespace=destination_namespace
        )

        try:
            check_vms_power_state(
                source_vm=source_vm,
                destination_vm=destination_vm,
                source_power_before_migration=vm.get("source_vm_power"),
            )
        except Exception as exp:
            res[vm_name].append(f"check_vms_power_state - {str(exp)}")

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
            check_storage(source_vm=source_vm, destination_vm=destination_vm, storage_map_resource=storage_map_resource)
        except Exception as exp:
            res[vm_name].append(f"check_storage - {str(exp)}")

        if source_provider.type == Provider.ProviderType.VSPHERE:
            if snapshots_before_migration := vm.get("snapshots_before_migration"):
                try:
                    check_snapshots(
                        snapshots_before_migration=snapshots_before_migration,
                        snapshots_after_migration=source_vm["snapshots_data"],
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_snapshots - {str(exp)}")

        if vm_guest_agent:
            try:
                check_guest_agent(destination_vm=destination_vm)
            except Exception as exp:
                res[vm_name].append(f"check_guest_agent - {str(exp)}")

        # SSH connectivity check - only when source VM was powered on
        if vm_ssh_connections and vm.get("source_vm_power") == "on":
            try:
                check_ssh_connectivity(
                    vm_name=vm_name,
                    vm_ssh_connections=vm_ssh_connections,
                    vm_config=vm,
                    source_provider_data=source_provider_data,
                    source_vm_info=source_vm,
                )
            except Exception as exp:
                res[vm_name].append(f"check_ssh_connectivity - {str(exp)}")

            # Static IP preservation check - only for Windows VMs with static IPs migrated from VSPHERE
            if (
                vm.get("source_vm_data")
                and vm["source_vm_data"].get("win_os")
                and source_provider.type == Provider.ProviderType.VSPHERE
            ):
                try:
                    check_static_ip_preservation(
                        vm_name=vm_name,
                        vm_ssh_connections=vm_ssh_connections,
                        source_vm_data=vm["source_vm_data"],
                        source_provider_data=source_provider_data,
                    )
                except Exception as exp:
                    res[vm_name].append(f"check_static_ip_preservation - {str(exp)}")
        elif vm_ssh_connections and vm.get("source_vm_power") != "on":
            LOGGER.info(
                f"Skipping SSH connectivity check for VM {vm_name} - source VM was not powered on (source_vm_power: "
                f"{vm.get('source_vm_power')})"
            )

        if rhv_provider(source_provider_data) and isinstance(source_provider, OvirtProvider):
            try:
                check_false_vm_power_off(source_provider=source_provider, source_vm=source_vm)
            except Exception as exp:
                res[vm_name].append(f"check_false_vm_power_off - {str(exp)}")

        for _vm_name, _errors in res.items():
            if _errors:
                should_fail = True
                LOGGER.error(f"VM {_vm_name} failed checks: {_errors}")

    if should_fail:
        pytest.fail("Some of the VMs did not match")
