"""Guest IP mode (NetworkMap networkIPMode) verification utilities.

Verifies per-network guest IP outcomes driven by NetworkMap entry-level
``networkIPMode`` (forklift MTV-5674). ``preserve`` is checked by
``check_static_ip_preservation``; ignored destinations by ``check_network``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ocp_resources.network_map import NetworkMap

from utilities.naming import resolve_destination_vm_name
from utilities.post_migration import (
    get_nic_by_mac,
    get_ssh_credentials_from_provider_config,
    load_current_interfaces,
    map_item_matches_source_network,
)
from utilities.ssh_utils import SSHConnectionManager, VMSSHConnection, run_cmd_in_vm
from utilities.utils import normalize_source_network

if TYPE_CHECKING:
    from libs.providers.openshift import OCPProvider

_WINDOWS_NETWORK_CMD = ["ipconfig", "/all"]
_LINUX_IP_ADDR_CMD = ["ip", "-j", "addr", "show"]
_LINUX_IP_ROUTE_CMD = ["ip", "-j", "route", "show"]
_LINUX_NMCLI_CONNECTIONS_CMD = ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"]


def _ipv4_addresses(interface: dict[str, Any]) -> list[str]:
    """Return IPv4 addresses reported by a guest interface dict.

    Args:
        interface (dict[str, Any]): Guest interface with ``ip_addresses`` entries.

    Returns:
        list[str]: IPv4 address strings.
    """
    return [ip_config["ip_address"] for ip_config in interface.get("ip_addresses", []) if ip_config.get("ip_address")]


def _map_item_for_nic(map_items: Any, nic: dict[str, Any]) -> Any:
    """Return the NetworkMap entry for a source NIC.

    Args:
        map_items (Any): ``spec.map`` from the NetworkMap.
        nic (dict[str, Any]): Source NIC with a network name/id.

    Returns:
        The matching map entry.

    Raises:
        ValueError: If no map entry matches the NIC's source network.
    """
    source_network = normalize_source_network(nic)
    matching = [map_item for map_item in map_items if map_item_matches_source_network(map_item, source_network)]
    if not matching:
        raise ValueError(f"No NetworkMap entry matches source network {source_network!r} for NIC '{nic['name']}'")
    return matching[0]


def _load_linux_guest_interfaces(ssh_conn: VMSSHConnection, vm_name: str) -> list[dict[str, Any]]:
    """Collect Linux guest interfaces via ``ip -j`` (includes addressless none-mode NICs).

    Args:
        ssh_conn (VMSSHConnection): Established SSH connection.
        vm_name (str): Destination VM name for command descriptions.

    Returns:
        list[dict[str, Any]]: Interfaces with ``name``, ``macAddress``, ``ip_addresses``,
            and ``default_gateways`` (same shape as the Windows ipconfig parse).

    Raises:
        GuestCommandError: If an ``ip`` command fails.
    """
    addr_output = run_cmd_in_vm(
        ssh_conn=ssh_conn, cmd=_LINUX_IP_ADDR_CMD, description=f"Show interfaces of VM {vm_name}"
    )
    route_output = run_cmd_in_vm(ssh_conn=ssh_conn, cmd=_LINUX_IP_ROUTE_CMD, description=f"Show routes of VM {vm_name}")
    gateways = {
        route["dev"]: route["gateway"]
        for route in json.loads(route_output)
        if route.get("dst") == "default" and route.get("gateway") and route.get("dev")
    }
    interfaces: list[dict[str, Any]] = []
    for link in json.loads(addr_output):
        if link.get("ifname") == "lo":
            continue
        mac_address = link.get("address", "")
        if ":" not in mac_address and "-" not in mac_address:
            continue
        ipv4_addresses = [
            addr_info["local"] for addr_info in link.get("addr_info", []) if addr_info.get("family") == "inet"
        ]
        gateway = gateways.get(link["ifname"])
        interfaces.append({
            "name": link["ifname"],
            "macAddress": mac_address,
            "ip_addresses": [{"ip_address": address} for address in ipv4_addresses],
            "default_gateways": [gateway] if gateway else [],
        })
    return interfaces


def _load_guest_interfaces(ssh_conn: VMSSHConnection, vm_name: str, is_windows: bool) -> list[dict[str, Any]] | None:
    """Load guest interfaces for the OS. Windows may return None when its output cannot be parsed.

    Args:
        ssh_conn (VMSSHConnection): Established SSH connection.
        vm_name (str): Destination VM name.
        is_windows (bool): True for ipconfig, False for ``ip -j``.

    Returns:
        list[dict[str, Any]] | None: Guest interfaces, or None when the Windows output cannot be parsed.

    Raises:
        GuestCommandError: If a guest command fails.
        CommandExecFailed: If the Windows network command fails.
    """
    if is_windows:
        parsed = load_current_interfaces(
            ssh_conn=ssh_conn,
            network_cmd=_WINDOWS_NETWORK_CMD,
            is_windows=True,
            vm_name=vm_name,
        )
        if parsed is None:
            return None
        return list(parsed.values())
    return _load_linux_guest_interfaces(ssh_conn=ssh_conn, vm_name=vm_name)


def _linux_connection_ipv4_settings(ssh_conn: VMSSHConnection, vm_name: str, device: str) -> str:
    """Return the NetworkManager ipv4.method of an active device's connection.

    Args:
        ssh_conn (VMSSHConnection): Established SSH connection.
        vm_name (str): Destination VM name.
        device (str): Interface name (e.g. ``eth1``).

    Returns:
        str: The connection's ipv4.method (e.g. "auto" for DHCP, "manual" for static).

    Raises:
        AssertionError: If the device has no active NM connection.
        GuestCommandError: If an nmcli command fails.
    """
    overview_output = run_cmd_in_vm(
        ssh_conn=ssh_conn,
        cmd=_LINUX_NMCLI_CONNECTIONS_CMD,
        description=f"Show active NetworkManager connections of VM {vm_name}",
    )
    profile_name = None
    for line in overview_output.splitlines():
        name, separator, overview_device = line.partition(":")
        if separator and name and overview_device == device:
            profile_name = name
            break
    assert profile_name, f"VM '{vm_name}': no active NetworkManager connection for device '{device}'"
    method_output = run_cmd_in_vm(
        ssh_conn=ssh_conn,
        cmd=["nmcli", "-g", "ipv4.method", "connection", "show", profile_name],
        description=f"Show IPv4 method of NetworkManager connection '{profile_name}' of VM {vm_name}",
    )
    return method_output.strip()


def _verify_none_interface(
    vm_name: str,
    source_nic: dict[str, Any],
    destination_vm: dict[str, Any],
    guest_interfaces: list[dict[str, Any]],
) -> None:
    """Assert a none-mode NIC exists on the destination VM and has no IPv4 address.

    Args:
        vm_name (str): Destination VM name.
        source_nic (dict[str, Any]): Source NIC on the none-mode network.
        destination_vm (dict[str, Any]): Destination VM data with network_interfaces.
        guest_interfaces (list[dict[str, Any]]): Guest interface list.

    Raises:
        AssertionError: If the interface holds an IPv4 address.
        ValueError: If no destination NIC or guest interface matches the MAC.
    """
    get_nic_by_mac(nics=destination_vm["network_interfaces"], mac_address=source_nic["macAddress"])
    interface = get_nic_by_mac(nics=guest_interfaces, mac_address=source_nic["macAddress"])
    addresses = _ipv4_addresses(interface)
    assert not addresses, (
        f"VM '{vm_name}': none-mode interface '{interface['name']}' (MAC {source_nic['macAddress']}) "
        f"holds IPv4 address(es) {addresses}; expected no address"
    )


def _verify_dhcp_interface(
    vm_name: str,
    source_nics: list[dict[str, Any]],
    source_nic: dict[str, Any],
    destination_vm: dict[str, Any],
    guest_interfaces: list[dict[str, Any]],
    ssh_conn: VMSSHConnection,
    is_windows: bool,
) -> None:
    """Assert a dhcp-mode NIC is DHCP-configured and has lost its source static settings.

    Args:
        vm_name (str): Destination VM name.
        source_nics (list[dict[str, Any]]): All source NICs on this dhcp-mode map entry.
        source_nic (dict[str, Any]): Source NIC being verified.
        destination_vm (dict[str, Any]): Destination VM data with network_interfaces.
        guest_interfaces (list[dict[str, Any]]): Guest interface list.
        ssh_conn (VMSSHConnection): Established SSH connection (Linux NM queries).
        is_windows (bool): True to use ipconfig ``dhcp_enabled``.

    Raises:
        AssertionError: If the interface is not DHCP configured or still holds source static settings.
        ValueError: If no destination NIC or guest interface matches the MAC.
        GuestCommandError: If an nmcli command fails.
    """
    get_nic_by_mac(nics=destination_vm["network_interfaces"], mac_address=source_nic["macAddress"])
    interface = get_nic_by_mac(nics=guest_interfaces, mac_address=source_nic["macAddress"])
    source_static_ips = [
        ip_config["ip_address"]
        for nic in source_nics
        for ip_config in nic.get("ip_addresses", [])
        if ip_config.get("is_static_ip")
    ]
    configured_addresses = _ipv4_addresses(interface)
    retained_static_ips = [address for address in configured_addresses if address in source_static_ips]
    assert not retained_static_ips, (
        f"VM '{vm_name}': dhcp-mode interface (MAC {source_nic['macAddress']}) still holds source "
        f"static address(es) {retained_static_ips}; expected them replaced by DHCP configuration"
    )
    if is_windows:
        assert interface.get("dhcp_enabled"), (
            f"VM '{vm_name}': dhcp-mode interface (MAC {source_nic['macAddress']}) is not DHCP enabled"
        )
        return
    method = _linux_connection_ipv4_settings(ssh_conn=ssh_conn, vm_name=vm_name, device=interface["name"])
    assert method == "auto", (
        f"VM '{vm_name}': dhcp-mode interface '{interface['name']}' (MAC {source_nic['macAddress']}) "
        f"is not DHCP configured (NetworkManager ipv4.method: {method!r}, expected 'auto')"
    )


def verify_network_map_network_ip_mode(
    network_map_resource: NetworkMap,
    network_map_overrides: list[dict[str, Any]],
) -> None:
    """Assert the live NetworkMap spec.map carries every override's destination and guest IP mode.

    Fails at map creation when the cluster prunes the networkIPMode field (e.g. an
    MTV without forklift MTV-5674) instead of silently skipping dhcp-mode verification.

    Args:
        network_map_resource (NetworkMap): Created NetworkMap resource.
        network_map_overrides (list[dict[str, Any]]): Overrides resolved to concrete source networks.
    """
    for override in network_map_overrides:
        source_network = override["source_network"]
        matching_entries = [
            map_item
            for map_item in network_map_resource.instance.spec.map
            if map_item_matches_source_network(map_item, source_network)
        ]
        assert matching_entries, f"Source network '{source_network}' has no NetworkMap entry"

        map_item = matching_entries[0]
        assert map_item.destination.type == override["destination"]["type"], (
            f"NetworkMap entry for source network '{source_network}' has destination type "
            f"'{map_item.destination.type}', expected '{override['destination']['type']}'"
        )

        expected_network_ip_mode = override.get("network_ip_mode")
        assert getattr(map_item, "networkIPMode", None) == expected_network_ip_mode, (
            f"NetworkMap entry for source network '{source_network}' has networkIPMode "
            f"{getattr(map_item, 'networkIPMode', None)!r}, expected {expected_network_ip_mode!r}; "
            f"the cluster may not support per-network guest IP configuration (forklift MTV-5674)"
        )


def verify_preserve_mode_dns_data(
    network_map_overrides: list[dict[str, Any]],
    source_vms_data: dict[str, Any],
) -> None:
    """Assert that source NICs on preserve-mode networks report DNS servers.

    ``check_static_ip_preservation`` skips DNS verification when the source data has
    no DNS servers; failing here keeps the advertised preserve outcome honest.

    Args:
        network_map_overrides (list[dict[str, Any]]): Overrides resolved to concrete source networks.
        source_vms_data (dict[str, Any]): Source VM data (from prepared_plan) keyed by VM name.
    """
    preserve_networks = [
        override["source_network"]
        for override in network_map_overrides
        if override.get("network_ip_mode") == "preserve"
    ]
    for vm_name, vm_data in source_vms_data.items():
        for nic in vm_data["network_interfaces"]:
            source_network = normalize_source_network(nic)
            if source_network in preserve_networks:
                assert any(ip_config.get("dns_servers") for ip_config in nic.get("ip_addresses", [])), (
                    f"VM '{vm_name}': NIC '{nic.get('name')}' on preserve-mode network '{source_network}' "
                    f"reports no DNS servers; DNS preservation cannot be verified (ensure the guest reports "
                    f"the NIC's DNS configuration — VMware Tools on Windows, the NM connection profile on Linux)"
                )


def _verify_vm_guest_ip_modes(
    vm_name: str,
    source_vm_data: dict[str, Any],
    destination_vm: dict[str, Any],
    network_map_resource: NetworkMap,
    source_provider_data: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
) -> None:
    """Verify guest IP mode outcomes of one migrated VM.

    Args:
        vm_name (str): Destination VM name.
        source_vm_data (dict[str, Any]): Source VM data (from prepared_plan).
        destination_vm (dict[str, Any]): Destination VM data with network_interfaces.
        network_map_resource (NetworkMap): NetworkMap resource.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        vm_ssh_connections (SSHConnectionManager): SSH connections fixture manager.

    Raises:
        ValueError: If a NIC has no map entry, or Windows is asked to verify none-mode.
        AssertionError: If a mode-specific expectation fails.
        GuestCommandError: If a guest command fails.
        CommandExecFailed: If the Windows network command fails.
    """
    is_windows = bool(source_vm_data.get("win_os", False))
    map_items = network_map_resource.instance.spec.map
    to_verify: list[tuple[str, Any, dict[str, Any]]] = []
    for nic in source_vm_data["network_interfaces"]:
        map_item = _map_item_for_nic(map_items, nic)
        if map_item.destination.type in ("ignored", "pod"):
            continue
        guest_ip_mode = getattr(map_item, "networkIPMode", None)
        if guest_ip_mode not in ("none", "dhcp"):
            continue
        if guest_ip_mode == "none" and is_windows:
            raise ValueError(
                f"none-mode guest IP verification is not implemented for Windows guests; "
                f"VM '{vm_name}' has a NIC on a network mapped with networkIPMode 'none'"
            )
        to_verify.append((guest_ip_mode, map_item, nic))

    if not to_verify:
        return

    ssh_username, ssh_password = get_ssh_credentials_from_provider_config(source_provider_data, source_vm_data)
    ssh_conn = vm_ssh_connections.create(vm_name=vm_name, username=ssh_username, password=ssh_password)

    with ssh_conn:
        guest_interfaces = _load_guest_interfaces(ssh_conn=ssh_conn, vm_name=vm_name, is_windows=is_windows)
        if guest_interfaces is None:
            raise ValueError(f"VM '{vm_name}': guest returned no interface configuration")

        for guest_ip_mode, map_item, nic in to_verify:
            if guest_ip_mode == "none":
                _verify_none_interface(
                    vm_name=vm_name,
                    source_nic=nic,
                    destination_vm=destination_vm,
                    guest_interfaces=guest_interfaces,
                )
            else:
                _verify_dhcp_interface(
                    vm_name=vm_name,
                    source_nics=[entry_nic for _, item, entry_nic in to_verify if item is map_item],
                    source_nic=nic,
                    destination_vm=destination_vm,
                    guest_interfaces=guest_interfaces,
                    ssh_conn=ssh_conn,
                    is_windows=is_windows,
                )


def verify_network_ip_modes(
    plan: dict[str, Any],
    network_map_resource: NetworkMap,
    destination_provider: "OCPProvider",
    source_provider_data: dict[str, Any],
    vm_ssh_connections: SSHConnectionManager,
) -> None:
    """Verify per-network guest IP mode outcomes on migrated VMs.

    Driven by the map entries (forklift MTV-5674): ``dhcp`` mode — DHCP enabled
    with source static settings gone; ``none`` mode — interface present with no
    IPv4 address (Linux guests). ``preserve`` is verified by
    ``check_static_ip_preservation`` and ignored destinations by ``check_network``.

    Args:
        plan (dict[str, Any]): Prepared plan with source_vms_data and _vm_target_namespace.
        network_map_resource (NetworkMap): NetworkMap resource resolving source networks to destinations.
        destination_provider (OCPProvider): Destination provider instance.
        source_provider_data (dict[str, Any]): Provider configuration from .providers.json.
        vm_ssh_connections (SSHConnectionManager): SSH connections fixture manager.

    Raises:
        ValueError: If a NIC has no map entry, the guest is Windows and a NIC is
            on a none-mode network, or the guest returns no interfaces.
        AssertionError: If any mode-specific expectation fails.
        GuestCommandError: If a guest command fails.
        CommandExecFailed: If the guest network configuration command fails.
    """
    for vm in plan["virtual_machines"]:
        destination_vm_name = resolve_destination_vm_name(vm)
        source_vm_data = plan["source_vms_data"][vm["name"]]
        destination_vm = destination_provider.vm_dict(
            wait_for_guest_agent=bool(vm.get("guest_agent")),
            name=destination_vm_name,
            namespace=plan["_vm_target_namespace"],
        )
        _verify_vm_guest_ip_modes(
            vm_name=destination_vm_name,
            source_vm_data=source_vm_data,
            destination_vm=destination_vm,
            network_map_resource=network_map_resource,
            source_provider_data=source_provider_data,
            vm_ssh_connections=vm_ssh_connections,
        )
