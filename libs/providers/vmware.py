from __future__ import annotations

import copy
import ipaddress
from typing import Any, Self

from ocp_resources.provider import Provider
from ocp_resources.resource import Resource
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import VmBadDatastoreError, VmCloneError, VmMissingVmxError, VmNotFoundError
from libs.base_provider import BaseProvider
from utilities.naming import generate_name_with_uuid

LOGGER = get_logger(__name__)

# VMware vSphere NIC device key offset
# In vSphere, network adapter device keys start at 4000
# The gateway routing table uses device IDs without this offset
# Reference: VMware vSphere API VirtualEthernetCard documentation
VSPHERE_NIC_DEVICE_KEY_OFFSET = 4000


class VMWareProvider(BaseProvider):
    """https://github.com/vmware/vsphere-automation-sdk-python"""

    DISK_TYPE_MAP = {
        "thin": ("sparse", "Setting disk provisioning to 'thin' (sparse)."),
        "thick-lazy": ("flat", "Setting disk provisioning to 'thick-lazy' (flat)."),
        "thick-eager": ("eagerZeroedThick", "Setting disk provisioning to 'thick-eager' (eagerZeroedThick)."),
    }
    DISK_PROVISION_TYPE_MAP = {
        "thin": {"thinProvisioned": True, "eagerlyScrub": False},
        "thick-lazy": {"thinProvisioned": False, "eagerlyScrub": False},
        "thick-eager": {"thinProvisioned": False, "eagerlyScrub": True},
    }

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        ocp_resource: Provider | None = None,
        **kwargs: Any,
    ) -> None:
        # Extract copyoffload configuration before calling parent
        self.copyoffload_config = kwargs.pop("copyoffload", {})

        super().__init__(ocp_resource=ocp_resource, host=host, username=username, password=password, **kwargs)

        self.type = Provider.ProviderType.VSPHERE
        self.host = host
        self.username = username
        self.password = password

    def disconnect(self) -> None:
        LOGGER.info(f"Disconnecting VMWareProvider source provider {self.host}")
        Disconnect(si=self.api)

    def connect(self) -> Self:
        self.api = SmartConnect(  # ssl cert check is not required
            host=self.host,
            user=self.username,
            pwd=self.password,
            port=443,
            disableSslCertValidation=True,
        )
        return self

    @property
    def test(self) -> bool:
        try:
            self.api.RetrieveContent().authorizationManager.description
            return True
        except Exception:
            return False

    @property
    def reconnect_if_not_connected(self) -> None:
        if not self.test:
            LOGGER.info("Reconnecting to VMware")
            self.connect()

    @property
    def content(self) -> vim.ServiceInstanceContent:
        return self.api.RetrieveContent()

    @property
    def view_manager(self) -> vim.view.ViewManager:
        view_manager = self.content.viewManager
        if not view_manager:
            raise ValueError("View manager is not available.")

        return view_manager

    def get_vm_by_name(
        self,
        query: str,
        vm_name_suffix: str = "",
        clone_vm: bool = False,
        session_uuid: str = "",
        clone_options: dict | None = None,
    ) -> vim.VirtualMachine:
        target_vm_name = f"{query}{vm_name_suffix}"
        target_vm = None
        try:
            target_vm = self.get_obj(vimtype=[vim.VirtualMachine], name=target_vm_name)
        except ValueError:
            if clone_vm:
                # Use copyoffload datastore if configured
                target_datastore_id = self.copyoffload_config.get("datastore_id")
                target_vm = self.clone_vm(
                    source_vm_name=query,
                    clone_vm_name=target_vm_name,
                    session_uuid=session_uuid,
                    target_datastore_id=target_datastore_id,
                    **(clone_options or {}),
                )
                if not target_vm:
                    raise VmNotFoundError(
                        f"Failed to clone VM '{target_vm_name}' by cloning from '{query}' on host [{self.host}]",
                    )
            else:
                # Re-raise the original error if cloning is not enabled
                raise

        if not target_vm:
            raise VmNotFoundError(f"VM {target_vm_name} not found on host [{self.host}]")

        # Perform health checks on the VM
        if self.is_vm_missing_vmx_file(vm=target_vm):
            raise VmMissingVmxError(vm=target_vm.name)

        if self.is_vm_with_bad_datastore(vm=target_vm):
            raise VmBadDatastoreError(vm=target_vm.name)

        return target_vm

    def wait_task(self, task: vim.Task, action_name: str, wait_timeout: int = 60, sleep: int = 1) -> Any:
        """Waits and provides updates on a vSphere task."""
        try:
            for sample in TimeoutSampler(
                wait_timeout=wait_timeout,
                sleep=sleep,
                func=lambda: task.info.state == vim.TaskInfo.State.success,
            ):
                if task.info.error:
                    error_msg = (
                        str(task.info.error.localizedMessage)
                        if hasattr(task.info.error, "localizedMessage")
                        else str(task.info.error)
                    )
                    raise VmCloneError(f"vSphere task failed: {error_msg}")

                if sample:
                    self.log.info(
                        msg=(
                            f"{action_name} completed successfully. "
                            f"{f'result: {task.info.result}' if task.info.result else ''}"
                        ),
                    )
                    return task.info.result

                try:
                    progress = f"{int(task.info.progress)}%" if task.info.progress else "In progress"
                except TypeError:
                    progress = "N/A"

                LOGGER.info("%s progress: %s", action_name, progress)
        except TimeoutExpiredError:
            self.log.error(msg=f"{action_name} did not complete successfully: {task.info.error}")
            raise

    def start_vm(self, vm):
        if vm.runtime.powerState != vm.runtime.powerState.poweredOn:
            self.wait_task(task=vm.PowerOn(), action_name=f"Starting VM {vm.name}")

    def stop_vm(self, vm):
        if vm.runtime.powerState == vm.runtime.powerState.poweredOn:
            self.wait_task(task=vm.PowerOff(), action_name=f"Stopping VM {vm.name}")

    @staticmethod
    def list_snapshots(vm):
        snapshots = []
        # vm.snapshot has no rootSnapshotList attribute if the VMWare VM does not have snapshots
        if hasattr(vm.snapshot, "rootSnapshotList"):
            root_snapshot_list = vm.snapshot.rootSnapshotList
            while root_snapshot_list:
                snapshot = root_snapshot_list[0]
                snapshots.append(snapshot)
                root_snapshot_list = snapshot.childSnapshotList
        return snapshots

    def _get_network_name_from_device(self, device: vim.vm.device.VirtualEthernetCard) -> str:
        """Extract network name from a virtual ethernet device.

        Handles different network backing types:
        - Standard network backing (vSwitch)
        - Distributed Virtual Switch (DVS) portgroups

        Args:
            device: Virtual ethernet card device

        Returns:
            str: Network name or "Unknown" if unable to determine

        """
        network_name = "Unknown"

        if not device.backing:
            return network_name

        # Standard network backing (vSwitch)
        if hasattr(device.backing, "network") and device.backing.network:
            return device.backing.network.name

        # Distributed virtual port backing (DVS)
        if hasattr(device.backing, "port") and device.backing.port:
            port = device.backing.port
            if hasattr(port, "portgroupKey"):
                # Resolve the portgroup key to its name by searching all DVS portgroups
                try:
                    container = self.view_manager.CreateContainerView(
                        self.content.rootFolder,
                        [vim.dvs.DistributedVirtualPortgroup],
                        True,
                    )
                    for pg in container.view:  # type: ignore[attr-defined]
                        if pg.key == port.portgroupKey:
                            network_name = pg.name
                            break
                    container.Destroy()

                    # If we didn't find it, fall back to the key
                    if network_name == "Unknown":
                        network_name = f"DVS-{port.portgroupKey}"
                except Exception:
                    # Fallback if we can't resolve the portgroup
                    network_name = f"DVS-{port.portgroupKey}"
            else:
                network_name = "Distributed Virtual Switch"

        return network_name

    def _extract_nic_ip_info(self, vm: vim.VirtualMachine, device: vim.vm.device.VirtualEthernetCard) -> dict[str, Any]:
        """Extract IP address information from a virtual network interface.

        This method retrieves IP configuration from VMware Tools guest information including:
        - IPv4 addresses (filters out link-local and IPv6)
        - Subnet masks (prefix lengths)
        - Default gateway (from guest routing table)
        - IP assignment method (static vs DHCP)

        Args:
            vm: VMware VM object
            device: Virtual ethernet card device

        Returns:
            dict: IP information containing 'ip_addresses' list or empty dict if no IP info available

        """
        result: dict[str, Any] = {}

        # Check if guest info is available
        if not (
            hasattr(vm, "guest")
            and vm.guest
            and hasattr(vm, "runtime")
            and vm.runtime.powerState == "poweredOn"
            and vm.guest.net
        ):
            # Log why guest info is not available for debugging
            reasons = []
            if not hasattr(vm, "guest") or not vm.guest:
                reasons.append("guest object not available")
            if not hasattr(vm, "runtime") or vm.runtime.powerState != "poweredOn":
                reasons.append(
                    f"VM not powered on (state: {
                        getattr(vm.runtime, 'powerState', 'unknown') if hasattr(vm, 'runtime') else 'no runtime'
                    })",
                )
            if hasattr(vm, "guest") and vm.guest and not vm.guest.net:
                reasons.append("guest network info not available")

            LOGGER.debug(f"VM {vm.name} NIC {device.deviceInfo.label}: Cannot get IP info - {', '.join(reasons)}")
            return result

        LOGGER.debug(f"Guest info available for VM {vm.name}, checking {len(vm.guest.net)} guest NICs")

        # Loop through guest NICs to find matching MAC address
        for guest_nic in vm.guest.net:
            LOGGER.debug(f"Guest NIC: MAC={guest_nic.macAddress}, Device MAC={device.macAddress}")

            # Match by MAC address
            if not (guest_nic.macAddress and guest_nic.macAddress.lower() == device.macAddress.lower()):
                LOGGER.debug("MAC addresses don't match")
                continue

            LOGGER.debug("  MAC addresses match, checking IP config")

            if not (guest_nic.ipConfig and guest_nic.ipConfig.ipAddress):
                LOGGER.debug("No IP config or IP addresses found for guest NIC")
                break

            LOGGER.debug(f"Found {len(guest_nic.ipConfig.ipAddress)} IP addresses")

            # Look for IPv4 addresses specifically
            ipv4_addresses = []
            for ip_info in guest_nic.ipConfig.ipAddress:
                LOGGER.debug(f"IP: {ip_info.ipAddress}, Origin: {getattr(ip_info, 'origin', 'N/A')}")
                try:
                    # Use built-in ipaddress module to check if it's IPv4
                    ip_obj = ipaddress.ip_address(ip_info.ipAddress)
                    if isinstance(ip_obj, ipaddress.IPv4Address) and not ip_obj.is_link_local:
                        ipv4_addresses.append(ip_info)
                        LOGGER.debug(f"IPv4 address found: {ip_info.ipAddress}")
                    else:
                        LOGGER.debug(f"Non-IPv4 or link-local address skipped: {ip_info.ipAddress}")
                except ValueError:
                    LOGGER.debug(f"Invalid IP address skipped: {ip_info.ipAddress}")

            # Store all IPv4 addresses found
            if not ipv4_addresses:
                LOGGER.warning(
                    f"VM {vm.name} NIC {device.deviceInfo.label}: No valid IPv4 addresses found "
                    f"(skipped link-local and non-IPv4)",
                )
                break

            # Initialize ip_addresses as a list to store multiple IP configurations
            result["ip_addresses"] = []

            # Try to get gateway information from guest IP stack routing table
            gateway_ip = None
            dns_servers = None
            if hasattr(vm.guest, "ipStack") and vm.guest.ipStack:
                # Look for default gateway routes (0.0.0.0/0) that match this NIC
                device_id = str(device.key - VSPHERE_NIC_DEVICE_KEY_OFFSET)

                for ip_stack in vm.guest.ipStack:
                    if hasattr(ip_stack, "ipRouteConfig") and ip_stack.ipRouteConfig:
                        for route in ip_stack.ipRouteConfig.ipRoute:
                            # Look for default routes (0.0.0.0/0) with IPv4 gateway
                            if (
                                route.network == "0.0.0.0"
                                and route.prefixLength == 0
                                and hasattr(route.gateway, "ipAddress")
                                and route.gateway.ipAddress
                                and route.gateway.device == device_id
                            ):
                                # Check if it's IPv4 (not IPv6)
                                try:
                                    ip_obj = ipaddress.ip_address(route.gateway.ipAddress)
                                    if isinstance(ip_obj, ipaddress.IPv4Address):
                                        gateway_ip = route.gateway.ipAddress
                                        LOGGER.info(
                                            f"VM {vm.name} NIC {device.deviceInfo.label}:"
                                            f" Gateway={route.gateway.ipAddress}",
                                        )
                                        break
                                except ValueError:
                                    LOGGER.info(
                                        f"VM {vm.name} NIC {device.deviceInfo.label}: Invalid"
                                        f" gateway IP {route.gateway.ipAddress}",
                                    )

                # Extract DNS servers if available
                for ip_stack in vm.guest.ipStack:
                    if hasattr(ip_stack, "dnsConfig") and ip_stack.dnsConfig:
                        dns_servers = ip_stack.dnsConfig.ipAddress
                        break

            # Process each IPv4 address
            for ip_info in ipv4_addresses:
                ip_config = {
                    "ip_address": ip_info.ipAddress,
                    "subnet_mask": ip_info.prefixLength,
                    "gateway": gateway_ip,  # Same gateway for all IPs on this interface
                }

                # Add DNS servers if available
                if dns_servers:
                    ip_config["dns_servers"] = dns_servers

                # Get definitive IP assignment method from VMware API
                if hasattr(ip_info, "origin"):
                    ip_config["ip_origin"] = ip_info.origin
                    ip_config["is_static_ip"] = ip_info.origin == "manual"
                    LOGGER.info(
                        f"VM {vm.name} NIC {device.deviceInfo.label}: IPv4={ip_info.ipAddress}"
                        f" Origin={ip_info.origin} Static={ip_info.origin == 'manual'}",
                    )
                else:
                    ip_config["ip_origin"] = None
                    ip_config["is_static_ip"] = None
                    LOGGER.warning(f"VM {vm.name} NIC {device.deviceInfo.label}: IP origin not available")

                result["ip_addresses"].append(ip_config)

            # Log summary of all IPv4 addresses found
            if len(ipv4_addresses) > 1:
                LOGGER.info(
                    f"Multiple IPv4 addresses found on {device.deviceInfo.label}: "
                    f"{[ip.ipAddress for ip in ipv4_addresses]}",
                )
            else:
                LOGGER.info(f"Single IPv4 address found on {device.deviceInfo.label}: {ipv4_addresses[0].ipAddress}")

            if not gateway_ip:
                LOGGER.info(
                    f"VM {vm.name} NIC {device.deviceInfo.label} Device ID {device.key}: "
                    f"No default gateway found for this NIC",
                )

            break  # Found matching MAC, exit loop

        return result

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        # If VM object already provided, use it directly (avoids re-searching for cloned VMs)
        _vm = kwargs.get("provider_vm_api")

        if not _vm:
            vm_name = kwargs["name"]
            _vm = self.get_vm_by_name(
                query=f"{vm_name}",
                vm_name_suffix=kwargs.get("vm_name_suffix", ""),
                clone_vm=kwargs.get("clone", False),
                session_uuid=kwargs.get("session_uuid", ""),
                clone_options=kwargs.get("clone_options"),
            )

        vm_config: Any = _vm.config
        if not vm_config:
            raise ValueError(f"No config found for VM {_vm.name}")

        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = Resource.ProviderType.VSPHERE
        result_vm_info["provider_vm_api"] = _vm
        result_vm_info["name"] = _vm.name
        result_vm_info["id"] = _vm._moId  # VMware Managed Object ID

        # Devices
        for device in vm_config.hardware.device:
            # Network Interfaces
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                network_name = self._get_network_name_from_device(device)
                nic_info = {
                    "name": device.deviceInfo.label if device.deviceInfo else "Unknown",
                    "macAddress": device.macAddress,
                    "network": {"name": network_name},
                }

                # Extract IP information using helper method
                ip_data = self._extract_nic_ip_info(vm=_vm, device=device)
                nic_info.update(ip_data)

                LOGGER.info(f"Final NIC info for VM {_vm.name} NIC {device.deviceInfo.label}: {nic_info}")
                result_vm_info["network_interfaces"].append(nic_info)

            # Disks
            if isinstance(device, vim.vm.device.VirtualDisk):
                result_vm_info["disks"].append({
                    "name": device.deviceInfo.label if device.deviceInfo else "Unknown",
                    "size_in_kb": device.capacityInKB,
                    "storage": dict(
                        name=device.backing.datastore.name
                        if device.backing and device.backing.datastore
                        else "Unknown",
                    ),
                    "device_key": device.key,
                    "unit_number": device.unitNumber,
                    "controller_key": device.controllerKey,
                })

        # CPUs
        result_vm_info["cpu"]["num_cores"] = vm_config.hardware.numCoresPerSocket
        result_vm_info["cpu"]["num_sockets"] = int(vm_config.hardware.numCPU / result_vm_info["cpu"]["num_cores"])

        # Memory
        result_vm_info["memory_in_mb"] = vm_config.hardware.memoryMB

        # Snapshots details
        for snapshot in self.list_snapshots(_vm):
            result_vm_info["snapshots_data"].append(
                dict({
                    "name": snapshot.name,
                    "id": snapshot.id,
                    "create_time": snapshot.createTime,
                    "state": snapshot.state,
                }),
            )

        # Guest Agent Status (bool)
        result_vm_info["guest_agent_running"] = (
            hasattr(_vm, "runtime")
            and _vm.runtime.powerState == "poweredOn"
            and _vm.guest
            and _vm.guest.toolsStatus == "toolsOk"
        )

        # Guest OS
        result_vm_info["win_os"] = "win" in vm_config.guestId

        # Power state
        if _vm.runtime.powerState == "poweredOn":
            result_vm_info["power_state"] = "on"
        elif _vm.runtime.powerState == "poweredOff":
            result_vm_info["power_state"] = "off"
        else:
            result_vm_info["power_state"] = "other"

        return result_vm_info

    def is_vm_missing_vmx_file(self, vm: vim.VirtualMachine) -> bool:
        if not vm.datastore:
            self.log.error(f"VM {vm.name} is inaccessible due to datastore error")
            return True

        if not vm.config:
            self.log.error(f"VM {vm.name} is inaccessible due to config error")
            return True

        search_spec = vim.host.DatastoreBrowser.SearchSpec()
        search_spec.matchPattern = ["*.vmx"]
        vm_datastore_info = vm.datastore[0].browser.SearchSubFolders(vm.config.files.vmPathName, search_spec)
        if vm_datastore_info.info.state == "error":
            _error = vm_datastore_info.info.error.msg

            if "vmx was not found" in _error:
                self.log.error(f"VM {vm.name} is inaccessible due to datastore error: {_error}")
                return True

        return False

    def is_vm_with_bad_datastore(self, vm: vim.VirtualMachine) -> bool:
        if vm.summary.runtime.connectionState == "inaccessible":
            self.log.error(f"VM {vm.name} is inaccessible due to connection error")
            return True
        return False

    def get_obj(self, vimtype: Any, name: str) -> Any:
        self.reconnect_if_not_connected
        container = self.view_manager.CreateContainerView(self.content.rootFolder, vimtype, True)
        try:
            # Access the view property which contains the managed objects
            managed_objects = getattr(container, "view", [])
            for obj in managed_objects:
                # Check by name first
                if obj.name == name:
                    return obj
                # For datastores, also check by MoRef ID
                if vimtype == [vim.Datastore] and hasattr(obj, "_moId") and obj._moId == name:
                    return obj

            raise ValueError(f"Object of type {vimtype} with name '{name}' not found.")

        finally:
            container.Destroy()

    def _get_add_disk_device_specs(
        self,
        source_vm: vim.VirtualMachine,
        disks_to_add: list[dict[str, Any]],
        clone_vm_name: str,
        target_datastore: vim.Datastore,
    ) -> list[vim.vm.device.VirtualDeviceSpec]:
        """Helper method to generate VirtualDeviceSpec for adding new disks.

        Args:
            source_vm: The source VM object.
            disks_to_add: List of dictionaries, each specifying details for a new disk.
            clone_vm_name: The name of the cloned VM.
            target_datastore: The datastore where the new disks should be created.

        Returns:
            A list of VirtualDeviceSpec objects for the new disks.

        """
        device_changes = []
        scsi_controller = next(
            (
                device
                for device in source_vm.config.hardware.device
                if isinstance(device, vim.vm.device.VirtualSCSIController)
            ),
            None,
        )
        if not scsi_controller:
            raise RuntimeError(f"Could not find a SCSI controller on VM '{source_vm.name}' to add new disks.")

        used_unit_numbers = {
            device.unitNumber
            for device in source_vm.config.hardware.device
            if device.controllerKey == scsi_controller.key
        }
        available_unit_number = next((i for i in range(16) if i != 7 and i not in used_unit_numbers), None)
        if available_unit_number is None:
            raise RuntimeError(f"No available unit number on SCSI controller for VM '{source_vm.name}'.")

        required_space_gb = sum(
            disk["size_gb"] for disk in disks_to_add if disk.get("provision_type", "thin").lower() != "thin"
        )
        available_space_gb = target_datastore.summary.freeSpace / (1024**3)

        if required_space_gb > 0:
            if required_space_gb > available_space_gb:
                raise VmCloneError(
                    f"Insufficient datastore capacity for thick-provisioned disks on '{target_datastore.name}'. "
                    f"Required: {required_space_gb:.2f} GB, Available: {available_space_gb:.2f} GB.",
                )
            LOGGER.info(
                f"Validating datastore capacity for thick disks. "
                f"Required: {required_space_gb:.2f} GB, Available on '{target_datastore.name}': {available_space_gb:.2f} GB",
            )
        else:
            LOGGER.info("No thick-provisioned disks to add; skipping datastore capacity check.")

        new_disk_key_counter = -101
        for disk in disks_to_add:
            new_disk_spec = vim.vm.device.VirtualDeviceSpec()
            new_disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            new_disk_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
            new_disk_spec.device = vim.vm.device.VirtualDisk()
            new_disk_spec.device.key = new_disk_key_counter
            new_disk_spec.device.controllerKey = scsi_controller.key
            new_disk_spec.device.unitNumber = available_unit_number
            new_disk_spec.device.capacityInKB = disk["size_gb"] * 1024 * 1024

            backing_info = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
            backing_info.diskMode = disk.get("disk_mode", "persistent")

            datastore_path = disk.get("datastore_path")
            if datastore_path:
                full_path = (
                    f"[{target_datastore.name}] {datastore_path}/{clone_vm_name}_disk_{available_unit_number}.vmdk"
                )
                LOGGER.info(f"Ensuring directory '[{target_datastore.name}] {datastore_path}' exists on datastore.")
                try:
                    file_manager = self.content.fileManager
                    datacenter = self.api.content.rootFolder.childEntity[0]
                    dir_path_for_creation = f"[{target_datastore.name}] {datastore_path}"
                    file_manager.MakeDirectory(
                        name=dir_path_for_creation,
                        datacenter=datacenter,
                        createParentDirectories=True,
                    )
                except (vim.fault.FileAlreadyExists, vim.fault.CannotCreateFile):
                    LOGGER.debug("Directory '%s' already exists, proceeding.", datastore_path)
                except Exception as e:
                    LOGGER.warning("Could not automatically create directory '%s': %s", datastore_path, e)
                LOGGER.info(f"Setting custom path for new disk on datastore '{target_datastore.name}': {full_path}")
                backing_info.fileName = full_path
            else:
                backing_info.fileName = f"[{target_datastore.name}]"

            provision_type_config = self.DISK_PROVISION_TYPE_MAP.get(
                disk.get("provision_type", "thin").lower(),
                self.DISK_PROVISION_TYPE_MAP["thin"],
            )
            backing_info.thinProvisioned = provision_type_config["thinProvisioned"]
            backing_info.eagerlyScrub = provision_type_config["eagerlyScrub"]
            LOGGER.info(f"Setting disk {available_unit_number} provisioning to: {disk.get('provision_type', 'thin')}")

            backing_info.datastore = target_datastore

            new_disk_spec.device.backing = backing_info
            device_changes.append(new_disk_spec)
            available_unit_number += 1
            new_disk_key_counter -= 1
        LOGGER.info(f"Configured {len(disks_to_add)} new disks for cloning")
        return device_changes

    def clone_vm(
        self,
        source_vm_name: str,
        clone_vm_name: str,
        session_uuid: str,
        power_on: bool = False,
        regenerate_mac: bool = True,
        **kwargs: Any,
    ) -> vim.VirtualMachine:
        """Clones a virtual machine from an existing VM or template.

        Args:
            source_vm_name: The name of the VM or template to clone from.
            clone_vm_name: The name for the new cloned VM.
            session_uuid: A unique identifier for the session, used for naming.
            power_on: Whether to power on the VM after cloning. Defaults to False.
            regenerate_mac: Whether to regenerate MAC addresses for network interfaces.
                          Prevents MAC address conflicts between cloned VMs. Default: True.
            **kwargs: Additional keyword arguments for cloning options.
                add_disks (list[dict]): A list of dictionaries, where each dict
                    defines a new disk to be added to the cloned VM.
                    Supported keys for each disk:
                    - 'size_gb' (int): The size of the disk in gigabytes.
                    - 'provision_type' (str): 'thin', 'thick-lazy', or 'thick-eager'.
                    - 'disk_mode' (str): e.g., 'persistent', 'independent_persistent'.
                    - 'datastore_path' (str, optional): A custom folder path on the datastore
                                        where the disk's .vmdk file should be placed.
                                        E.g., "shared_disks". If not provided, defaults
                                        to the VM's main folder.
                target_datastore_id (str, optional): The MoRef ID of the specific datastore
                                        to use for the cloned VM and all its disks.
                                        If not provided, defaults to the source VM's datastore.

        Returns:
            vim.VirtualMachine: The cloned VM object.

        """
        clone_vm_name = generate_name_with_uuid(f"{session_uuid}-{clone_vm_name}")
        LOGGER.info("Starting clone process for '%s' from '%s'", clone_vm_name, source_vm_name)

        source_vm = self.get_obj([vim.VirtualMachine], source_vm_name)

        relocate_spec = vim.vm.RelocateSpec()

        target_datastore_id = kwargs.get("target_datastore_id")
        if target_datastore_id:
            target_datastore = self.get_obj([vim.Datastore], target_datastore_id)
            LOGGER.info(f"Using target datastore from config: {target_datastore.name} ({target_datastore_id})")
        elif source_vm.datastore:
            target_datastore = source_vm.datastore[0]
            LOGGER.info(f"Using source VM's default datastore: {target_datastore.name}")
        else:
            raise VmCloneError(f"Could not determine a target datastore for cloning '{source_vm_name}'.")

        relocate_spec.datastore = target_datastore  # Ensure relocate_spec uses the determined datastore

        # If the source is a template, it may not have a resource pool; find a suitable one from the cluster.
        if not source_vm.resourcePool:
            container = self.view_manager.CreateContainerView(self.content.rootFolder, [vim.ComputeResource], True)
            view = container.view  # type: ignore[attr-defined]
            relocate_spec.pool = next((cr.resourcePool for cr in view if cr.resourcePool), None)
            container.Destroy()
        else:
            relocate_spec.pool = source_vm.resourcePool

        if not relocate_spec.pool:
            raise VmCloneError("Could not determine a valid resource pool for cloning.")

        config_spec = vim.vm.ConfigSpec()
        device_changes = []

        disk_type = kwargs.get("disk_type")
        if disk_type:
            disk_config = self.DISK_TYPE_MAP.get(disk_type.lower())
            if disk_config:
                relocate_spec.transform, log_message = disk_config
                LOGGER.info(log_message)
            else:
                LOGGER.warning("Disk type '%s' not recognized. Using vSphere default.", disk_type)

        disks_to_add = kwargs.get("add_disks")
        if disks_to_add:
            disk_device_specs = self._get_add_disk_device_specs(
                source_vm=source_vm,
                disks_to_add=disks_to_add,
                clone_vm_name=clone_vm_name,
                target_datastore=target_datastore,
            )
            device_changes.extend(disk_device_specs)

        if regenerate_mac:
            source_config = source_vm.config
            if source_config and source_config.hardware and source_config.hardware.device:
                for device in source_config.hardware.device:
                    if isinstance(device, vim.vm.device.VirtualEthernetCard):
                        device_spec = vim.vm.device.VirtualDeviceSpec()
                        device_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
                        device_spec.device = device
                        device_spec.device.addressType = "generated"
                        device_changes.append(device_spec)
                        LOGGER.info(
                            f"Configured MAC regeneration for network device: {
                                device.deviceInfo.label if device.deviceInfo else 'Unknown'
                            }",
                        )

        if device_changes:
            config_spec.deviceChange = device_changes
            LOGGER.info(f"Applied {len(device_changes)} device changes to the clone specification.")

        # Enable Change Block Tracking (CBT) for warm migration support
        cbt_option = vim.option.OptionValue()
        cbt_option.key = "ctkEnabled"
        cbt_option.value = "true"
        if config_spec.extraConfig:
            config_spec.extraConfig.append(cbt_option)
        else:
            config_spec.extraConfig = [cbt_option]
        LOGGER.info("Enabling Change Block Tracking (CBT) on cloned VM '%s'", clone_vm_name)

        clone_spec = vim.vm.CloneSpec(
            location=relocate_spec,
            powerOn=power_on,
            template=False,
            config=config_spec,
        )

        task = source_vm.CloneVM_Task(folder=source_vm.parent, name=clone_vm_name, spec=clone_spec)
        LOGGER.info("Clone task started for %s. Waiting for completion...", clone_vm_name)

        try:
            res = self.wait_task(
                task=task,
                action_name=f"Cloning VM {clone_vm_name} from {source_vm_name}",
                wait_timeout=60 * 20,
                sleep=5,
            )
        except VmCloneError as e:
            if regenerate_mac and "in use" in str(e).lower():
                LOGGER.warning("Clone failed with resource conflict, retrying without MAC regeneration")
                if clone_spec.config and clone_spec.config.deviceChange:
                    non_mac_changes = [
                        change
                        for change in clone_spec.config.deviceChange
                        if not isinstance(change.device, vim.vm.device.VirtualEthernetCard)
                    ]
                    if non_mac_changes:
                        clone_spec.config.deviceChange = non_mac_changes
                    else:
                        # Preserve extraConfig (CBT) by clearing only deviceChange
                        clone_spec.config.deviceChange = []
                task = source_vm.CloneVM_Task(folder=source_vm.parent, name=clone_vm_name, spec=clone_spec)
                res = self.wait_task(
                    task=task,
                    action_name=f"Cloning VM {clone_vm_name} from {source_vm_name} (retry)",
                    wait_timeout=60 * 20,
                    sleep=5,
                )
            else:
                raise

        if res and self.fixture_store:
            self.fixture_store["teardown"].setdefault(self.type, []).append({"name": clone_vm_name})
        return res

    def delete_vm(self, vm_name: str) -> None:
        vm = self.get_obj(vimtype=[vim.VirtualMachine], name=vm_name)
        self.stop_vm(vm=vm)
        task = vm.Destroy_Task()
        self.wait_task(task=task, action_name=f"Deleting VM {vm_name}")

    def wait_for_vmware_guest_info(self, vm: vim.VirtualMachine, timeout: int = 60) -> bool:
        """Wait for VMware guest information to become available after VM power-on.

        Args:
            vm: VMware VM object (vim.VirtualMachine)
            timeout: Maximum time to wait in seconds (default: 60)

        Returns:
            bool: True if guest info becomes available, False if timeout

        """
        LOGGER.info(f"Waiting for VMware Tools guest info for VM {vm.name} (timeout: {timeout}s)")

        try:
            for sample in TimeoutSampler(
                wait_timeout=timeout,
                sleep=5,
                func=lambda: (
                    hasattr(vm, "guest")
                    and vm.guest
                    and hasattr(vm, "runtime")
                    and vm.runtime.powerState == "poweredOn"
                    and vm.guest.net
                    and len(vm.guest.net) > 0
                    and vm.guest.toolsStatus in ["toolsOk", "toolsOld"]  # Accept toolsOld too
                ),
            ):
                if sample:
                    LOGGER.info(
                        f"VMware Tools guest info available for VM {vm.name} (tools status: {vm.guest.toolsStatus})",
                    )
                    return True

        except Exception as e:
            LOGGER.warning(f"Error waiting for guest info on VM {vm.name}: {e}")
            return False

        # Log diagnostic info only on timeout
        power_state = getattr(vm.runtime, "powerState", "unknown") if hasattr(vm, "runtime") else "no runtime"
        tools_status = getattr(vm.guest, "toolsStatus", "unknown") if hasattr(vm, "guest") and vm.guest else "no guest"
        net_count = len(vm.guest.net) if hasattr(vm, "guest") and vm.guest and vm.guest.net else 0
        LOGGER.warning(
            f"Timeout waiting for VMware Tools guest info on VM {vm.name} after {timeout}s "
            f"(power={power_state}, tools={tools_status}, networks={net_count})",
        )
        return False
