from __future__ import annotations

import copy
import ipaddress
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import requests
from ocp_resources.provider import Provider
from pypsrp.exceptions import WinRMError
from pypsrp.powershell import PowerShell, RunspacePool
from pypsrp.wsman import WSMan
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import PowerShellCommandError, VmCloneError, VmNotFoundError
from libs.base_provider import BaseProvider

if TYPE_CHECKING:
    from libs.forklift_inventory import ForkliftInventory
    from pypsrp.complex_objects import GenericComplexObject

LOGGER = get_logger(__name__)


def _ps_prop(obj: GenericComplexObject, name: str) -> Any:
    """Access a property from a pypsrp GenericComplexObject.

    Args:
        obj: pypsrp deserialized GenericComplexObject
        name: Property name (e.g., "Name", "VMId")

    Returns:
        Property value

    Raises:
        KeyError: If property not found in adapted or extended properties
    """
    try:
        return obj.adapted_properties[name]
    except KeyError:
        return obj.extended_properties[name]


class HyperVProvider(BaseProvider):
    """Microsoft Hyper-V provider using PowerShell Remoting Protocol (PSRP)."""

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        """Normalize MAC address to colon-separated lowercase format.

        Args:
            mac: MAC address in any format (no separators, dashes, colons).

        Returns:
            Colon-separated lowercase MAC (e.g., "00:15:5d:01:02:03").
        """
        mac_clean = mac.replace("-", "").replace(":", "").replace(".", "").lower()
        return ":".join(mac_clean[i : i + 2] for i in range(0, len(mac_clean), 2))

    @staticmethod
    def _first_ipv4_gateway(gateways: list[str]) -> str | None:
        """Return the first IPv4 gateway from a list of gateway addresses.

        Args:
            gateways: List of gateway address strings, which may include IPv6.

        Returns:
            str | None: First IPv4 gateway address, or None if none found.
        """
        for gateway in gateways:
            if ":" not in gateway:
                return gateway
        return None

    @staticmethod
    def _subnet_to_prefix_length(subnet: str) -> int | None:
        """Convert a KVP subnet string to an integer prefix length.

        Args:
            subnet: Subnet mask in dotted-decimal ("255.255.255.0") or prefix
                length ("24") format, as reported by Hyper-V KVP. May be
                empty or malformed (e.g., "/") during KVP initialization.

        Returns:
            int | None: Prefix length (e.g., 24), or None if the value is
            empty or malformed (e.g., during KVP initialization).
        """
        cleaned = subnet.strip().strip("/")
        if not cleaned:
            return None
        if cleaned.isdigit():
            return int(cleaned)
        try:
            return ipaddress.IPv4Network(f"0.0.0.0/{cleaned}", strict=False).prefixlen
        except (ValueError, ipaddress.AddressValueError):
            return None

    @staticmethod
    def _ensure_list(value: list | str | None) -> list:
        """Coerce a JSON value into a list.

        PowerShell's ``ConvertTo-Json`` flattens single-element arrays into
        bare strings at the default serialization depth. This guard ensures
        downstream code always iterates over list items, not characters.

        Args:
            value: JSON-decoded value that should represent a list, but may
                have been flattened to a bare string or be ``None``/empty.

        Returns:
            list: The original list, a single-element list wrapping a string
            value, or an empty list when the value is falsy.
        """
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        return value

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        ocp_resource: Provider | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            ocp_resource=ocp_resource,
            host=host,
            username=username,
            password=password,
            **kwargs,
        )
        self.type = Provider.ProviderType.HYPERV
        self._wsman: WSMan | None = None
        self._pool: RunspacePool | None = None

    def connect(self) -> Self:
        """Establish connection to Hyper-V host via PSRP.

        Returns:
            Self: Connected provider instance.
        """
        self._wsman = WSMan(
            server=self.host,
            username=self.username,
            password=self.password,
            auth="ntlm",
            ssl=True,
            port=5986,
            cert_validation=False,
        )
        self._pool = RunspacePool(self._wsman)
        self._pool.open()
        return self

    def disconnect(self) -> None:
        """Close PSRP connection to Hyper-V host."""
        if self._pool:
            self._pool.close()
            self._pool = None
        if self._wsman:
            self._wsman.close()
            self._wsman = None

    @property
    def test(self) -> bool:
        """Test if connection is alive.

        Returns:
            bool: True if connection is alive, False otherwise.
        """
        try:
            self._run_cmdlet("Get-Command", Name="Get-VM")
            return True
        except (ConnectionError, PowerShellCommandError, WinRMError, requests.exceptions.RequestException):
            return False

    def _run_cmdlet(self, cmdlet: str, **params: Any) -> list[Any]:
        """Execute a PowerShell cmdlet with parameters.

        Args:
            cmdlet: PowerShell cmdlet name (e.g., "Get-VM")
            **params: Cmdlet parameters as keyword arguments

        Returns:
            List of output objects from PowerShell

        Raises:
            ConnectionError: If connection to the Hyper-V host is not established
            PowerShellCommandError: If cmdlet execution fails
        """
        if not self._pool:
            raise ConnectionError("Not connected to Hyper-V host. Call connect() first.")

        ps = PowerShell(self._pool)
        ps.add_cmdlet(cmdlet)
        if params:
            ps.add_parameters(params)

        output = ps.invoke()

        if ps.had_errors:
            error_msg = "\n".join(str(err) for err in ps.streams.error)
            raise PowerShellCommandError(f"PowerShell cmdlet '{cmdlet}' failed: {error_msg}")

        return output

    def _run_script(self, script: str) -> list[Any]:
        """Execute a PowerShell script (for multi-step operations).

        Args:
            script: PowerShell script text

        Returns:
            List of output objects from PowerShell

        Raises:
            ConnectionError: If connection to the Hyper-V host is not established
            PowerShellCommandError: If script execution fails
        """
        if not self._pool:
            raise ConnectionError("Not connected to Hyper-V host. Call connect() first.")

        ps = PowerShell(self._pool)
        ps.add_script(script)
        output = ps.invoke()

        if ps.had_errors:
            error_msg = "\n".join(str(err) for err in ps.streams.error)
            raise PowerShellCommandError(f"PowerShell script failed: {error_msg}")

        return output

    def get_vm_by_name(
        self,
        query: str,
        vm_name_suffix: str = "",
        clone_vm: bool = False,
        session_uuid: str = "",
        clone_options: dict[str, Any] | None = None,
    ) -> Any:
        """Get VM by name, optionally cloning if not found.

        Args:
            query: VM name to search for.
            vm_name_suffix: Suffix to append for the target VM name.
            clone_vm: If True, clone from source VM if target not found.
            session_uuid: Session UUID for clone naming.
            clone_options: Additional clone configuration options.

        Returns:
            VM object from Hyper-V (pypsrp deserialized CIM instance).

        Raises:
            VmNotFoundError: If VM not found and clone_vm is False.
        """
        clone_options = clone_options or {}
        clone_name = clone_options.get("clone_name")
        if isinstance(clone_name, str) and clone_name.strip():
            target_vm_name = clone_name
        else:
            target_vm_name = f"{query}{vm_name_suffix}"

        try:
            vms = self._run_cmdlet("Get-VM", Name=target_vm_name, ErrorAction="Stop")
            if vms:
                return vms[0]
        except PowerShellCommandError:
            if clone_vm:
                clone_vm_options = {k: v for k, v in clone_options.items() if k != "clone_name"}
                cloned = self.clone_vm(
                    source_vm_name=query,
                    clone_vm_name=target_vm_name,
                    session_uuid=session_uuid,
                    **clone_vm_options,
                )
                if not cloned:
                    raise VmNotFoundError(f"Failed to clone VM '{target_vm_name}' from '{query}' on host [{self.host}]")
                return cloned
            raise VmNotFoundError(f"VM '{target_vm_name}' not found on Hyper-V host {self.host}")

        raise VmNotFoundError(f"VM '{target_vm_name}' not found on Hyper-V host {self.host}")

    def start_vm(self, vm: Any) -> None:
        """Start a VM if not already running.

        Args:
            vm: VM object from Hyper-V (pypsrp deserialized CIM instance).
        """
        vm_name = _ps_prop(vm, "Name")
        current_state = self._run_cmdlet("Get-VM", Name=vm_name)[0]
        if str(_ps_prop(current_state, "State")) != "Running":
            LOGGER.info(f"Starting VM '{vm_name}'")
            self._run_cmdlet("Start-VM", Name=vm_name)

    def stop_vm(self, vm: Any) -> None:
        """Stop a VM forcefully if not already off.

        Args:
            vm: VM object from Hyper-V (pypsrp deserialized CIM instance).
        """
        vm_name = _ps_prop(vm, "Name")
        current_state = self._run_cmdlet("Get-VM", Name=vm_name)[0]
        if str(_ps_prop(current_state, "State")) != "Off":
            LOGGER.info(f"Stopping VM '{vm_name}'")
            self._run_cmdlet("Stop-VM", Name=vm_name, Force=True)

    def is_vm_powered_on(self, vm: Any) -> bool:
        """Check whether a VM is currently powered on.

        Args:
            vm: VM object from Hyper-V (pypsrp deserialized CIM instance).

        Returns:
            bool: True if the VM is in the "Running" state, False otherwise.
        """
        vm_name = _ps_prop(vm, "Name")
        current_state = self._run_cmdlet("Get-VM", Name=vm_name)[0]
        return str(_ps_prop(current_state, "State")) == "Running"

    def list_snapshots(self, vm_name: str) -> list[Any]:
        """List all snapshots for a VM.

        Args:
            vm_name: Name of VM

        Returns:
            List of snapshot objects
        """
        return self._run_cmdlet("Get-VMSnapshot", VMName=vm_name, ErrorAction="SilentlyContinue")

    def wait_for_guest_network_config(self, vm_name: str, timeout: int = 120) -> None:
        """Wait for Hyper-V Integration Services to report guest network configuration.

        Polls the KVP exchange data until guest network config is available.
        This is needed after starting a VM to ensure Integration Services have
        reported IP configuration before collecting static IP data.

        Args:
            vm_name (str): Name of the VM to wait for.
            timeout (int): Maximum wait time in seconds.

        Raises:
            TimeoutError: If KVP data is not available within the timeout.
        """
        LOGGER.info(
            f"Waiting for Hyper-V Integration Services guest network config for VM '{vm_name}' (timeout: {timeout}s)"
        )
        try:
            for sample in TimeoutSampler(
                wait_timeout=timeout,
                sleep=5,
                func=self._has_kvp_data,
                vm_name=vm_name,
            ):
                if sample:
                    LOGGER.info(f"Guest network configuration (KVP) available for VM '{vm_name}'")
                    return
        except TimeoutExpiredError as e:
            raise TimeoutError(
                f"Timed out after {timeout}s waiting for Hyper-V Integration Services guest network "
                f"config for VM '{vm_name}'"
            ) from e

    def _has_kvp_data(self, vm_name: str) -> bool:
        """Check whether Hyper-V Integration Services KVP data is available for a VM.

        Args:
            vm_name: Name of VM to check.

        Returns:
            bool: True if at least one NIC has KVP-derived DHCP status available.
        """
        return any(
            nic["dhcp_enabled"] is not None and nic["kvp_ip_addresses"]
            for nic in self._get_nic_details(vm_name=vm_name)
        )

    def _get_nic_details(self, vm_name: str) -> list[dict[str, Any]]:
        """Query NIC details enriched with Hyper-V Integration Services KVP data.

        Combines `Get-VMNetworkAdapter` (name, MAC, switch, IP addresses) with
        `Msvm_GuestNetworkAdapterConfiguration` (DHCP status, subnets, gateways,
        DNS) from the Hyper-V Data Exchange (KVP) Integration Service, matched by
        MAC address via a 4-hop CIM traversal (`Msvm_ComputerSystem` ->
        `Msvm_VirtualSystemSettingData` -> `Msvm_SyntheticEthernetPortSettingData`
        -> `Msvm_GuestNetworkAdapterConfiguration`), in a single PowerShell script.

        Args:
            vm_name: Name of VM to query.

        Returns:
            list[dict[str, Any]]: One entry per NIC with keys "name",
            "mac_address", "switch_name", "ip_addresses", "kvp_ip_addresses",
            "dhcp_enabled", "subnets", "default_gateways", and "dns_servers".
            "kvp_ip_addresses" is the KVP-sourced parallel array to "subnets"
            (both from `Msvm_GuestNetworkAdapterConfiguration`), while
            "ip_addresses" comes from `Get-VMNetworkAdapter` and is only used
            as a fallback when KVP data is unavailable. When Integration
            Services data is unavailable (VM off, Integration Services
            disabled, or query failure), "dhcp_enabled" is None and
            "subnets"/"default_gateways"/"dns_servers"/"kvp_ip_addresses" are
            empty lists.
        """
        script = f"""
        $nics = Get-VMNetworkAdapter -VMName '{vm_name}'
        $kvp = @{{}}
        $vmCim = Get-CimInstance -Namespace root\\virtualization\\v2 `
            -ClassName Msvm_ComputerSystem -Filter "ElementName='{vm_name}'"
        if ($vmCim) {{
            $vs = Get-CimAssociatedInstance -InputObject $vmCim `
                -ResultClassName Msvm_VirtualSystemSettingData |
                Where-Object {{$_.VirtualSystemType -eq 'Microsoft:Hyper-V:System:Realized'}}
            if ($vs) {{
                Get-CimAssociatedInstance -InputObject $vs `
                    -ResultClassName Msvm_SyntheticEthernetPortSettingData | ForEach-Object {{
                    $port = $_
                    Get-CimAssociatedInstance -InputObject $port `
                        -ResultClassName Msvm_GuestNetworkAdapterConfiguration | ForEach-Object {{
                        $kvp[$port.Address] = $_
                    }}
                }}
            }}
        }}
        $result = foreach ($nic in $nics) {{
            $gc = $kvp[$nic.MacAddress]
            [PSCustomObject]@{{
                Name = $nic.Name
                MacAddress = $nic.MacAddress
                SwitchName = if ($nic.SwitchName) {{ $nic.SwitchName }} else {{ '' }}
                IPAddresses = @($nic.IPAddresses)
                KVPIPAddresses = if ($gc) {{ @($gc.IPAddresses) }} else {{ @() }}
                DHCPEnabled = if ($gc) {{ $gc.DHCPEnabled }} else {{ $null }}
                Subnets = if ($gc) {{ @($gc.Subnets) }} else {{ @() }}
                DefaultGateways = if ($gc) {{ @($gc.DefaultGateways) }} else {{ @() }}
                DNSServers = if ($gc) {{ @($gc.DNSServers) }} else {{ @() }}
            }}
        }}
        if (-not $result) {{ $result = @() }}
        ConvertTo-Json -Compress -Depth 4 -InputObject @($result)
        """

        try:
            output = self._run_script(script)
        except PowerShellCommandError as e:
            LOGGER.warning(f"Failed to query NIC details for VM '{vm_name}': {e}")
            return []

        if not output:
            LOGGER.warning(f"No NIC details output returned for VM '{vm_name}'")
            return []

        # pypsrp returns primitive string output either as a plain str or as a
        # GenericComplexObject whose string representation is the raw JSON.
        raw_json = output[-1]
        if not isinstance(raw_json, str):
            raw_json = str(raw_json)

        try:
            entries = json.loads(raw_json)
        except json.JSONDecodeError as e:
            LOGGER.warning(f"Failed to parse NIC details JSON for VM '{vm_name}': {e}")
            return []

        if isinstance(entries, dict):
            entries = [entries]
        entries = [entry for entry in entries if isinstance(entry, dict)]

        nic_details: list[dict[str, Any]] = []
        for entry in entries:
            mac_address = entry.get("MacAddress")
            if not mac_address:
                continue
            dhcp_enabled = entry.get("DHCPEnabled")
            nic_details.append({
                "name": entry.get("Name"),
                "mac_address": mac_address,
                "switch_name": entry.get("SwitchName") or "",
                "ip_addresses": HyperVProvider._ensure_list(entry.get("IPAddresses")),
                "kvp_ip_addresses": HyperVProvider._ensure_list(entry.get("KVPIPAddresses")),
                "dhcp_enabled": None if dhcp_enabled is None else bool(dhcp_enabled),
                "subnets": HyperVProvider._ensure_list(entry.get("Subnets")),
                "default_gateways": HyperVProvider._ensure_list(entry.get("DefaultGateways")),
                "dns_servers": HyperVProvider._ensure_list(entry.get("DNSServers")),
            })

        if not nic_details:
            LOGGER.warning(f"No NIC details found for VM '{vm_name}'")

        return nic_details

    def _build_nic_ip_entries(self, guest_config: dict[str, Any]) -> list[dict[str, Any]]:
        """Build enriched IPv4 address entries from KVP guest network configuration.

        Uses KVP's own `kvp_ip_addresses` and `subnets` (both produced by the
        same `_get_nic_details` query, from
        `Msvm_GuestNetworkAdapterConfiguration`), which are guaranteed to be
        parallel arrays. `Get-VMNetworkAdapter.IPAddresses` (the `ip_addresses`
        key) is not used here because it may include IPv6 addresses and is not
        guaranteed to be ordered the same way as the KVP subnets.

        Args:
            guest_config: KVP-derived network config for a NIC, as produced by
                `_get_nic_details`.

        Returns:
            list[dict[str, Any]]: Enriched IPv4 address entries with subnet mask,
            gateway, origin, and static-IP flag. IPv6 addresses are excluded.
            "subnet_mask" is None when the corresponding KVP subnet entry is
            missing, empty, or malformed (e.g., "/" during KVP initialization).
        """
        dhcp_enabled = guest_config["dhcp_enabled"]
        # KVP DHCPEnabled is unreliable on RHEL 9+ guests using NetworkManager
        # keyfiles: hv_kvp_daemon only checks ifcfg files, so all NICs report
        # DHCPEnabled=False. Fixed in RHEL 10. See README for details.
        ip_origin = "dhcp" if dhcp_enabled else "manual"
        kvp_ips = guest_config["kvp_ip_addresses"]
        subnets = guest_config["subnets"]
        gateway = self._first_ipv4_gateway(guest_config["default_gateways"])

        entries: list[dict[str, Any]] = []
        for index, ip in enumerate(kvp_ips):
            if ":" in ip:
                continue
            subnet_mask = self._subnet_to_prefix_length(subnets[index]) if index < len(subnets) else None
            entries.append({
                "ip_address": ip,
                "subnet_mask": subnet_mask,
                "gateway": gateway,
                "ip_origin": ip_origin,
                "is_static_ip": not dhcp_enabled,
            })
        return entries

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        """Build normalized VM data dictionary.

        Args:
            **kwargs: Must contain 'name' key for VM name.
                     Optional: 'clone', 'session_uuid', 'provider_vm_api' (to reuse VM object)

        Returns:
            Dictionary with normalized VM data

        Raises:
            VmNotFoundError: If VM not found
        """
        # Reuse VM object if provided
        vm_obj = kwargs.get("provider_vm_api")

        if not vm_obj:
            vm_name = kwargs["name"]
            vm_obj = self.get_vm_by_name(
                query=vm_name,
                clone_vm=kwargs.get("clone", False),
                session_uuid=kwargs.get("session_uuid", ""),
                vm_name_suffix=kwargs.get("vm_name_suffix", ""),
                clone_options=kwargs.get("clone_options"),
            )

        vm_name = _ps_prop(vm_obj, "Name")

        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = self.type
        result_vm_info["provider_vm_api"] = vm_obj
        result_vm_info["name"] = vm_name
        result_vm_info["id"] = str(_ps_prop(vm_obj, "VMId"))

        # CPU - Hyper-V exposes total vCPUs only
        result_vm_info["cpu"]["num_cores"] = int(_ps_prop(vm_obj, "ProcessorCount"))
        result_vm_info["cpu"]["num_sockets"] = 1

        # Memory - convert bytes to MB
        result_vm_info["memory_in_mb"] = int(_ps_prop(vm_obj, "MemoryStartup")) // (1024 * 1024)

        # Power state
        result_vm_info["power_state"] = "on" if str(_ps_prop(vm_obj, "State")) == "Running" else "off"

        # Firmware - Gen1=BIOS, Gen2=UEFI
        firmware_data: dict[str, Any] = {}
        generation = _ps_prop(vm_obj, "Generation")
        if generation == 1:
            firmware_data["boot_firmware"] = "bios"
            firmware_data["secure_boot"] = False
            firmware_data["tpm_present"] = False
        elif generation == 2:
            firmware_data["boot_firmware"] = "efi"
            firmware_data["secure_boot"] = False
            firmware_data["tpm_present"] = False
            try:
                fw_info = self._run_cmdlet("Get-VMFirmware", VMName=vm_name)
                if fw_info:
                    firmware_data["secure_boot"] = str(_ps_prop(fw_info[0], "SecureBoot")) == "On"

                sec_info = self._run_cmdlet("Get-VMSecurity", VMName=vm_name)
                if sec_info:
                    firmware_data["tpm_present"] = bool(_ps_prop(sec_info[0], "TpmEnabled"))
            except PowerShellCommandError as e:
                LOGGER.warning(f"Failed to query firmware details for VM '{vm_name}': {e}")

        result_vm_info["firmware"] = firmware_data

        # Network Interfaces
        for nic in self._get_nic_details(vm_name=vm_name):
            nic_data: dict[str, Any] = {
                "name": nic["name"],
                "macAddress": self._normalize_mac(nic["mac_address"]),
                "network": {"name": nic["switch_name"]},
            }
            if nic["dhcp_enabled"] is not None:
                # KVP provides its own kvp_ip_addresses/subnets as guaranteed
                # parallel arrays; build entries entirely from KVP data (don't
                # mix sources).
                ipv4_addrs = self._build_nic_ip_entries(guest_config=nic)
            else:
                # No KVP data available: fall back to bare IPs from Get-VMNetworkAdapter.
                ipv4_addrs = [{"ip_address": ip} for ip in nic["ip_addresses"] if ":" not in ip]
            if ipv4_addrs:
                nic_data["ip_addresses"] = ipv4_addrs

            result_vm_info["network_interfaces"].append(nic_data)

        # Disks
        disk_drives = self._run_cmdlet("Get-VMHardDiskDrive", VMName=vm_name)
        for idx, disk in enumerate(disk_drives):
            # Get disk size via Get-VHD
            try:
                vhd_info = self._run_cmdlet("Get-VHD", Path=_ps_prop(disk, "Path"))
                if vhd_info:
                    size_bytes = int(_ps_prop(vhd_info[0], "Size"))
                    size_kb = size_bytes // 1024
                else:
                    size_kb = 0
            except PowerShellCommandError:
                size_kb = 0

            disk_path = str(_ps_prop(disk, "Path"))
            result_vm_info["disks"].append({
                "name": Path(disk_path).name,
                "size_in_kb": size_kb,
                "storage": {"name": str(Path(disk_path).parent)},
                "device_key": str(disk.adapted_properties.get("DiskNumber", idx)),
            })

        # Snapshots
        snapshots = self.list_snapshots(vm_name=vm_name)
        for snapshot in snapshots:
            result_vm_info["snapshots_data"].append({
                "name": _ps_prop(snapshot, "Name"),
                "id": str(_ps_prop(snapshot, "Id")),
                "create_time": str(_ps_prop(snapshot, "CreationTime")),
            })

        # Guest Agent - check for "Guest Service Interface" integration service
        try:
            integration_services = self._run_cmdlet("Get-VMIntegrationService", VMName=vm_name)
            guest_agent_enabled = any(
                _ps_prop(svc, "Name") == "Guest Service Interface" and _ps_prop(svc, "Enabled")
                for svc in integration_services
            )
            result_vm_info["guest_agent_running"] = guest_agent_enabled
        except PowerShellCommandError:
            result_vm_info["guest_agent_running"] = False

        # OS type heuristic - check VM notes or VM name
        # Hyper-V doesn't expose OS type directly, so we use a simple heuristic
        vm_notes = str(vm_obj.adapted_properties.get("Notes", ""))
        result_vm_info["win_os"] = "windows" in vm_notes.lower() or "win" in vm_name.lower()

        return result_vm_info

    def clone_vm(
        self,
        source_vm_name: str,
        clone_vm_name: str,
        session_uuid: str,
        **kwargs: Any,
    ) -> Any:
        """Clone a VM using Export-VM and Import-VM.

        Args:
            source_vm_name: Name of source VM to clone
            clone_vm_name: Name for the cloned VM
            session_uuid: Session UUID for unique naming
            **kwargs: Additional clone options

        Returns:
            Cloned VM object

        Raises:
            VmCloneError: If clone operation fails
        """
        clone_vm_name = self._generate_clone_vm_name(session_uuid=session_uuid, base_name=clone_vm_name)
        LOGGER.info(f"Cloning VM '{source_vm_name}' to '{clone_vm_name}'")

        # Create temp directory name on Hyper-V host
        temp_export_path = f"C:\\Temp\\HyperV-Clone-{session_uuid}"

        script = f"""
        $ErrorActionPreference = "Stop"

        # Ensure export directory exists
        New-Item -ItemType Directory -Path '{temp_export_path}' -Force | Out-Null

        # Get source VM
        $sourceVM = Get-VM -Name '{source_vm_name}'
        if (-not $sourceVM) {{
            throw "Source VM '{source_vm_name}' not found"
        }}

        # Check if VM is running
        $wasRunning = $sourceVM.State -eq 'Running'

        # Stop VM if running
        if ($wasRunning) {{
            Stop-VM -Name '{source_vm_name}' -Force
            Start-Sleep -Seconds 5
        }}

        # Export VM
        $exportPath = '{temp_export_path}'
        Export-VM -Name '{source_vm_name}' -Path $exportPath

        # Find .vmcx config file
        $vmcxPath = Get-ChildItem -Path $exportPath -Recurse -Filter "*.vmcx" | Select-Object -First 1
        if (-not $vmcxPath) {{
            throw "No .vmcx file found in export path"
        }}

        # Import VM as copy with new ID, redirecting VHDs to a path alongside the
        # source VM (same parent directory, different folder) to avoid colliding
        # with the source VM's disk files
        $clonePath = Join-Path (Split-Path $sourceVM.Path -Parent) '{clone_vm_name}'
        $cloneVhdPath = Join-Path $clonePath 'Virtual Hard Disks'
        New-Item -ItemType Directory -Path $cloneVhdPath -Force | Out-Null
        $clonedVM = Import-VM -Path $vmcxPath.FullName -Copy -GenerateNewId -VhdDestinationPath $cloneVhdPath -VirtualMachinePath $clonePath

        # Rename VM
        Rename-VM -VM $clonedVM -NewName '{clone_vm_name}'

        # Restart source VM if it was running
        if ($wasRunning) {{
            Start-VM -Name '{source_vm_name}'
        }}

        # Clean up export folder
        Remove-Item -Path $exportPath -Recurse -Force -ErrorAction SilentlyContinue

        # Trigger dynamic MAC assignment: Hyper-V assigns MACs from its pool at
        # VM start; without this, cloned NICs retain 00:00:00:00:00:00.
        Start-VM -Name '{clone_vm_name}'
        Stop-VM -Name '{clone_vm_name}' -Force -TurnOff

        # Return cloned VM
        Get-VM -Name '{clone_vm_name}'
        """

        try:
            result = self._run_script(script)
            if not result:
                raise PowerShellCommandError("Clone script did not return a VM object")

            cloned_vm = result[-1]  # Last output is the Get-VM result

            # Track cloned VM for cleanup
            if self.fixture_store:
                self.fixture_store["teardown"].setdefault(self.type, []).append({"name": clone_vm_name})

            LOGGER.info(f"Successfully cloned VM '{source_vm_name}' to '{clone_vm_name}'")
            return cloned_vm

        except PowerShellCommandError as e:
            LOGGER.error(f"Failed to clone VM '{source_vm_name}': {e}")
            try:
                cleanup_script = f"Remove-Item -Path '{temp_export_path}' -Recurse -Force -ErrorAction SilentlyContinue"
                self._run_script(cleanup_script)
            except PowerShellCommandError:
                pass
            raise VmCloneError(f"VM clone failed for '{clone_vm_name}': {e}") from e

    def delete_vm(self, vm_name: str) -> None:
        """Delete a VM and its files.

        Args:
            vm_name: Name of VM to delete
        """
        LOGGER.info(f"Deleting VM '{vm_name}'")

        script = f"""
        $ErrorActionPreference = "Stop"

        # Get VM (silently continue if not found)
        $vm = Get-VM -Name '{vm_name}' -ErrorAction SilentlyContinue
        if (-not $vm) {{
            exit 0
        }}

        # Stop VM if running
        if ($vm.State -eq 'Running') {{
            Stop-VM -Name '{vm_name}' -Force -TurnOff
            Start-Sleep -Seconds 3
        }}

        # Get VM paths before deletion
        $vmPath = $vm.Path
        $vhdPaths = @()
        Get-VMHardDiskDrive -VMName '{vm_name}' | ForEach-Object {{
            $vhdPaths += $_.Path
        }}

        # Remove VM
        Remove-VM -Name '{vm_name}' -Force

        # Clean up VHD files
        foreach ($vhdPath in $vhdPaths) {{
            if (Test-Path $vhdPath) {{
                Remove-Item -Path $vhdPath -Force -ErrorAction SilentlyContinue
            }}
        }}

        # Clean up VM folder
        if ($vmPath -and (Test-Path $vmPath)) {{
            Remove-Item -Path $vmPath -Recurse -Force -ErrorAction SilentlyContinue
        }}
        """

        self._run_script(script)

    def get_vm_or_template_networks(
        self,
        names: list[str],
        inventory: ForkliftInventory,
    ) -> list[dict[str, str]]:
        """Delegate to Forklift inventory for Hyper-V VMs.

        Args:
            names: List of VM names to query
            inventory: Forklift inventory instance

        Returns:
            List of network mappings
        """
        return inventory.vms_networks_mappings(vms=names)
