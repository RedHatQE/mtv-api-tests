from __future__ import annotations

import copy
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


class VMWareProvider(BaseProvider):
    """
    https://github.com/vmware/vsphere-automation-sdk-python
    """

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
        self, host: str, username: str, password: str, ocp_resource: Provider | None = None, **kwargs: Any
    ) -> None:
        # Extract copyoffload configuration before calling parent
        self.copyoffload_config: dict[str, Any] = kwargs.pop("copyoffload", {})

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
                # Use copyoffload datastore if configured (use first datastore from list as default)
                datastore_ids = self.copyoffload_config.get("datastore_ids", [])
                target_datastore_id = datastore_ids[0] if datastore_ids else None
                target_vm = self.clone_vm(
                    source_vm_name=query,
                    clone_vm_name=target_vm_name,
                    session_uuid=session_uuid,
                    target_datastore_id=target_datastore_id,
                    clone_options=clone_options,
                )
                if not target_vm:
                    raise VmNotFoundError(
                        f"Failed to clone VM '{target_vm_name}' by cloning from '{query}' on host [{self.host}]"
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
        """
        Waits and provides updates on a vSphere task.
        """
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
                        )
                    )
                    return task.info.result

                try:
                    progress = f"{int(task.info.progress)}%" if task.info.progress else "In progress"
                except TypeError:
                    progress = "N/A"

                LOGGER.info(f"{action_name} progress: {progress}")
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
        """
        Extract network name from a virtual ethernet device.

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
                        self.content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True
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

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
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

        # Devices
        for device in vm_config.hardware.device:
            # Network Interfaces
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                network_name = self._get_network_name_from_device(device)
                result_vm_info["network_interfaces"].append({
                    "name": device.deviceInfo.label if device.deviceInfo else "Unknown",
                    "macAddress": device.macAddress,
                    "network": {"name": network_name},
                })

            # Disks
            if isinstance(device, vim.vm.device.VirtualDisk):
                result_vm_info["disks"].append({
                    "name": device.deviceInfo.label if device.deviceInfo else "Unknown",
                    "size_in_kb": device.capacityInKB,
                    "storage": dict(
                        name=device.backing.datastore.name if device.backing and device.backing.datastore else "Unknown"
                    ),
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
                })
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
        datastore_map: dict[str, Any] | None = None,
    ) -> list[vim.vm.device.VirtualDeviceSpec]:
        """
        Helper method to generate VirtualDeviceSpec for adding new disks.

        Args:
            source_vm: The source VM object.
            disks_to_add: List of dictionaries, each specifying details for a new disk.
            datastore_map: Optional dictionary mapping datastore IDs to datastore objects.
                If provided, will be used to resolve datastore_ids items to datastore objects.

        Returns:
            A list of VirtualDeviceSpec objects for the new disks.
        """
        # 1. Pre-calculate required space and check datastore capacity for thick disks
        # Group by datastore to check capacity per datastore
        datastore_requirements: dict[str, float] = {}
        for disk in disks_to_add:
            if disk.get("provision_type", "thin").lower() != "thin":
                disk_datastore_id = disk.get("datastore_id")
                if disk_datastore_id:
                    datastore_requirements[disk_datastore_id] = (
                        datastore_requirements.get(disk_datastore_id, 0) + disk["size_gb"]
                    )
                else:
                    # Use source VM's datastore if not specified
                    default_datastore_id = source_vm.datastore[0]._moId if source_vm.datastore else None
                    if default_datastore_id:
                        datastore_requirements[default_datastore_id] = (
                            datastore_requirements.get(default_datastore_id, 0) + disk["size_gb"]
                        )

        # Validate capacity for each datastore
        for datastore_id, required_space_gb in datastore_requirements.items():
            if datastore_map and datastore_id in datastore_map:
                datastore = datastore_map[datastore_id]
            else:
                # Try to get datastore by ID
                try:
                    datastore = self.get_obj([vim.Datastore], datastore_id)
                except ValueError:
                    # Fall back to source VM's datastore
                    datastore = source_vm.datastore[0] if source_vm.datastore else None
                    if not datastore:
                        LOGGER.warning(f"Could not resolve datastore {datastore_id}, skipping capacity check")
                        continue

            free_space_gb = datastore.summary.freeSpace / (1024**3)
            LOGGER.info(
                f"Validating datastore capacity for thick disks. "
                f"Required: {required_space_gb:.2f} GB, "
                f"Available on '{datastore.name}': {free_space_gb:.2f} GB"
            )
            if required_space_gb > free_space_gb:
                raise VmCloneError(
                    f"Insufficient free space on datastore '{datastore.name}' for thick-provisioned disks. "
                    f"Required: {required_space_gb:.2f} GB, Available: {free_space_gb:.2f} GB"
                )

        # 1. Find a suitable SCSI controller on the source VM
        scsi_controller = next(
            (dev for dev in source_vm.config.hardware.device if isinstance(dev, vim.vm.device.VirtualSCSIController)),
            None,
        )
        if not scsi_controller:
            raise RuntimeError(f"Could not find a SCSI controller on VM '{source_vm.name}' to add new disks.")

        # 2. Find all currently used unit numbers on that controller
        used_unit_numbers = {
            dev.unitNumber for dev in source_vm.config.hardware.device if dev.controllerKey == scsi_controller.key
        }

        # 3. Find the first available unit number (0-15, excluding 7 which is reserved for the controller)
        available_unit_number = next((i for i in range(16) if i != 7 and i not in used_unit_numbers), None)
        if available_unit_number is None:
            raise RuntimeError(f"No available unit number on SCSI controller for VM '{source_vm.name}'.")

        device_specs = []
        new_disk_key = -101

        # 4. Create a spec for each disk to be added
        for disk_config in disks_to_add:
            spec = vim.vm.device.VirtualDeviceSpec()
            spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create

            disk_device = vim.vm.device.VirtualDisk()
            disk_device.key = new_disk_key
            disk_device.controllerKey = scsi_controller.key
            disk_device.unitNumber = available_unit_number
            disk_device.capacityInKB = disk_config["size_gb"] * 1024 * 1024

            backing_info = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
            backing_info.diskMode = disk_config.get("disk_mode", "persistent")

            provision_type = disk_config.get("provision_type", "thin").lower()
            provision_config = self.DISK_PROVISION_TYPE_MAP.get(provision_type)

            if provision_config:
                backing_info.thinProvisioned = provision_config["thinProvisioned"]
                backing_info.eagerlyScrub = provision_config["eagerlyScrub"]
                LOGGER.info(f"Setting disk {available_unit_number} provisioning to: {provision_type}")
            else:
                backing_info.thinProvisioned = True
                LOGGER.warning(
                    f"Disk provisioning type '{provision_type}' not recognized for disk {available_unit_number}. "
                    f"Defaulting to 'thin'."
                )

            # Set datastore for this disk if specified
            disk_datastore_id = disk_config.get("datastore_id")
            if disk_datastore_id:
                if datastore_map and disk_datastore_id in datastore_map:
                    target_datastore = datastore_map[disk_datastore_id]
                else:
                    # Resolve datastore by ID
                    try:
                        target_datastore = self.get_obj([vim.Datastore], disk_datastore_id)
                        if datastore_map is None:
                            datastore_map = {}
                        datastore_map[disk_datastore_id] = target_datastore
                    except ValueError:
                        LOGGER.warning(
                            f"Could not find datastore '{disk_datastore_id}' for disk {available_unit_number}. "
                            f"Using default datastore."
                        )
                        target_datastore = None

                if target_datastore:
                    backing_info.datastore = target_datastore
                    # Set fileName to force disk creation on the specified datastore
                    # Format: [datastore_name] vm_name/disk_name.vmdk
                    backing_info.fileName = f"[{target_datastore.name}]"
                    LOGGER.info(
                        f"Setting disk {available_unit_number} datastore to: {target_datastore.name} ({disk_datastore_id})"
                    )

            disk_device.backing = backing_info
            spec.device = disk_device
            device_specs.append(spec)

            # Increment for the next disk
            available_unit_number += 1
            new_disk_key -= 1

        LOGGER.info(f"Configured {len(disks_to_add)} new disks for cloning")
        return device_specs

    def clone_vm(self, source_vm_name: str, clone_vm_name: str, session_uuid: str, **kwargs: Any) -> vim.VirtualMachine:
        """
        Clones a VM from a source VM or template.

        Args:
            source_vm_name: The name of the VM or template to clone from.
            clone_vm_name: The name of the new VM to be created.
            power_on: Whether to power on the VM after cloning.
            regenerate_mac: Whether to regenerate MAC addresses for network interfaces.
                          Prevents MAC address conflicts between cloned VMs. Default: True.
        """

        # generate new uuid for uniqueness of a test
        clone_vm_name = generate_name_with_uuid(f"{session_uuid}-{clone_vm_name}")
        LOGGER.info(f"Starting clone process for '{clone_vm_name}' from '{source_vm_name}'")

        source_vm = self.get_obj([vim.VirtualMachine], source_vm_name)

        relocate_spec = vim.vm.RelocateSpec()
        # Explicitly set the datastore for the entire clone operation to ensure correct disk provisioning
        if source_vm.datastore:
            target_datastore = source_vm.datastore[0]
            relocate_spec.datastore = target_datastore
            LOGGER.info(f"Setting target datastore for clone to: {target_datastore.name}")
        else:
            LOGGER.warning(
                f"Source VM '{source_vm_name}' has no datastores. Datastore for clone will be chosen by vSphere."
            )

        relocate_spec.pool = source_vm.resourcePool

        # If source is a template, it usually has no resource pool, so we pick one from the environment
        if not relocate_spec.pool:
            container = self.view_manager.CreateContainerView(self.content.rootFolder, [vim.ComputeResource], True)
            view = container.view  # type: ignore[attr-defined]
            relocate_spec.pool = next((cr.resourcePool for cr in view if cr.resourcePool), None)
            container.Destroy()

        if not relocate_spec.pool:
            raise VmCloneError("Could not determine a valid resource pool for cloning.")

        clone_spec = vim.vm.CloneSpec(location=relocate_spec, powerOn=False, template=False)

        clone_options = kwargs.get("clone_options") or {}
        disk_type = clone_options.get("disk_type")
        config_spec = vim.vm.ConfigSpec()
        device_changes = []

        # Handle disk provisioning type
        if disk_type:
            disk_config = self.DISK_TYPE_MAP.get(disk_type.lower())
            if disk_config:
                relocate_spec.transform, log_message = disk_config
                LOGGER.info(log_message)
            else:
                LOGGER.warning(f"Disk type '{disk_type}' not recognized. Using vSphere default.")

        # Handle adding new disks by calling the helper method
        disks_to_add = clone_options.get("add_disks")
        if disks_to_add:
            # Resolve special datastore markers (e.g., "SECONDARY_DATASTORE")
            # This allows test configs to use markers that get resolved to actual datastore IDs
            datastore_ids = self.copyoffload_config.get("datastore_ids", [])

            for disk_config in disks_to_add:
                disk_datastore_id = disk_config.get("datastore_id")  # type: ignore[union-attr]

                # If no datastore_id specified, use primary datastore if available
                if disk_datastore_id is None:
                    if datastore_ids:
                        disk_config["datastore_id"] = datastore_ids[0]  # type: ignore[index]
                        LOGGER.info(f"No datastore_id specified for disk, using primary datastore: {datastore_ids[0]}")
                # Resolve SECONDARY_DATASTORE marker to actual secondary datastore ID
                elif disk_datastore_id == "SECONDARY_DATASTORE":
                    if len(datastore_ids) >= 2:
                        disk_config["datastore_id"] = datastore_ids[1]  # type: ignore[index]
                        LOGGER.info(f"Resolved SECONDARY_DATASTORE marker to datastore ID: {datastore_ids[1]}")
                    else:
                        LOGGER.warning(
                            f"SECONDARY_DATASTORE marker found but only {len(datastore_ids)} datastore(s) configured. "
                            "Using default datastore."
                        )
                        disk_config["datastore_id"] = None  # type: ignore[index]

            # Build datastore map for disk datastore resolution
            datastore_map: dict[str, Any] = {}
            # Pre-resolve any datastore IDs mentioned in disk configs
            for disk_config in disks_to_add:
                disk_datastore_id = disk_config.get("datastore_id")  # type: ignore[union-attr]
                if disk_datastore_id and disk_datastore_id not in datastore_map:
                    try:
                        datastore_map[disk_datastore_id] = self.get_obj([vim.Datastore], disk_datastore_id)
                        LOGGER.info(
                            f"Resolved datastore ID '{disk_datastore_id}' to datastore '{datastore_map[disk_datastore_id].name}'"
                        )
                    except ValueError:
                        LOGGER.warning(f"Could not resolve datastore ID '{disk_datastore_id}', will use default")

            disk_device_specs = self._get_add_disk_device_specs(source_vm, disks_to_add, datastore_map)
            device_changes.extend(disk_device_specs)

        # Handle VM configuration overrides (CPU, Memory, etc.) from the 'config' key
        if "config" in clone_options:
            vm_config_overrides = clone_options["config"]

            if "numCPUs" in vm_config_overrides:
                # This part of the code was not provided in the edit_specification,
                # so it's not included in the new_code.
                pass  # Placeholder for future implementation

        # Use target datastore if specified, otherwise relay on vsphere's default behaviour
        target_datastore_id = kwargs.get("target_datastore_id")
        if target_datastore_id:
            target_datastore = self.get_obj([vim.Datastore], target_datastore_id)
            relocate_spec.datastore = target_datastore
            LOGGER.info(f"Using target datastore: {target_datastore_id}")

        clone_spec.location = relocate_spec
        clone_spec.powerOn = kwargs.get("power_on", False)
        clone_spec.template = False

        # Configure MAC address regeneration if requested
        regenerate_mac = kwargs.get("regenerate_mac", True)
        if regenerate_mac:
            source_config = source_vm.config

            if source_config and source_config.hardware and source_config.hardware.device:
                for device in source_config.hardware.device:
                    if isinstance(device, vim.vm.device.VirtualEthernetCard):
                        # Create device spec for MAC regeneration
                        device_spec = vim.vm.device.VirtualDeviceSpec()
                        device_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.edit
                        device_spec.device = device
                        # Set address type to generate new MAC address
                        device_spec.device.addressType = "generated"
                        device_changes.append(device_spec)
                        LOGGER.info(
                            f"Configured MAC regeneration for network device: {device.deviceInfo.label if device.deviceInfo else 'Unknown'}"
                        )

        # Add device changes to clone spec if any
        if device_changes:
            config_spec.deviceChange = device_changes
            clone_spec.config = config_spec
            LOGGER.info(f"Applied {len(device_changes)} device changes to the clone specification.")

        task = source_vm.CloneVM_Task(folder=source_vm.parent, name=clone_vm_name, spec=clone_spec)
        LOGGER.info(f"Clone task started for {clone_vm_name}. Waiting for completion...")

        try:
            res = self.wait_task(
                task=task,
                action_name=f"Cloning VM {clone_vm_name} from {source_vm_name}",
                wait_timeout=60 * 20,
                sleep=5,
            )
        except VmCloneError as e:
            # Retry without MAC regeneration if we hit a resource conflict error
            if regenerate_mac and "in use" in str(e).lower():
                LOGGER.warning("Clone failed with resource conflict, retrying without MAC regeneration")

                # On retry, remove only the MAC regeneration settings from the clone_spec
                if clone_spec.config and clone_spec.config.deviceChange:
                    non_mac_changes = [
                        change
                        for change in clone_spec.config.deviceChange
                        if not isinstance(change.device, vim.vm.device.VirtualEthernetCard)
                    ]
                    if non_mac_changes:
                        clone_spec.config.deviceChange = non_mac_changes
                    else:
                        clone_spec.config = None  # Unset config if no other changes are left

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
            self.fixture_store["teardown"].setdefault(self.type, []).append({
                "name": clone_vm_name,
            })
        return res

    def delete_vm(self, vm_name: str) -> None:
        vm = self.get_obj(vimtype=[vim.VirtualMachine], name=vm_name)
        self.stop_vm(vm=vm)
        task = vm.Destroy_Task()
        self.wait_task(task=task, action_name=f"Deleting VM {vm_name}")
