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

LOGGER = get_logger(__name__)


class VMWareProvider(BaseProvider):
    """
    https://github.com/vmware/vsphere-automation-sdk-python
    """

    def __init__(
        self, host: str, username: str, password: str, ocp_resource: Provider | None = None, **kwargs: Any
    ) -> None:
        # Extract copyoffload configuration before calling parent
        self.copyoffload_config = kwargs.pop('copyoffload', {})

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
        self, query: str, vm_name_suffix: str = "", clone_vm: bool = False, session_uuid: str = ""
    ) -> vim.VirtualMachine:
        target_vm_name = f"{query}{vm_name_suffix}"
        target_vm = None
        try:
            target_vm = self.get_obj(vimtype=[vim.VirtualMachine], name=target_vm_name)
        except ValueError:
            if clone_vm:
                try:
                    # Use copyoffload datastore if configured
                    target_datastore_id = self.copyoffload_config.get('datastore_id')
                    target_vm = self.clone_vm(
                        source_vm_name=query,
                        clone_vm_name=target_vm_name,
                        session_uuid=session_uuid,
                        target_datastore_id=target_datastore_id
                    )
                    if not target_vm:
                        raise VmNotFoundError(
                            f"Failed to clone VM '{target_vm_name}' by cloning from '{query}' on host [{self.host}]"
                        )
                except Exception as clone_error:
                    raise VmNotFoundError(
                        f"Failed to clone VM '{target_vm_name}' by cloning from '{query}': {clone_error}"
                    ) from clone_error
            else:
                # Re-raise the original error if cloning is not enabled
                raise

        if not target_vm:
            raise VmNotFoundError(f"VM {target_vm_name} not found on host [{self.host}]")

        return target_vm

    def get_vms_by_cluster(self, cluster_name: str) -> list[vim.VirtualMachine]:
        """
        Get all VMs from a specific cluster.
        """
        LOGGER.info(f"Attempting to find VMs in cluster '{cluster_name}'")
        try:
            # Find the cluster by name
            cluster_obj = self.get_obj([vim.ClusterComputeResource], cluster_name)
            if not cluster_obj:
                raise ValueError(f"Cluster '{cluster_name}' not found.")

            # Create a container view for the cluster's VMs
            container = self.view_manager.CreateContainerView(cluster_obj, [vim.VirtualMachine], True)
            vms = container.view
            container.Destroy()
            LOGGER.info(f"Found {len(vms)} VMs in cluster '{cluster_name}'")
            return vms
        except Exception as e:
            LOGGER.error(f"Failed to get VMs from cluster '{cluster_name}': {e}")
            raise ValueError(f"Could not find VMs in cluster '{cluster_name}'") from e

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
                    error_msg = str(task.info.error.localizedMessage) if hasattr(task.info.error, 'localizedMessage') else str(task.info.error)
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

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        vm_name = kwargs["name"]

        _vm = self.get_vm_by_name(
            query=f"{vm_name}",
            vm_name_suffix=kwargs.get("vm_name_suffix", ""),
            clone_vm=kwargs.get("clone", False),
            session_uuid=kwargs.get("session_uuid", ""),
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
                # Handle different network backing types
                network_name = "Unknown"
                if device.backing:
                    if hasattr(device.backing, 'network') and device.backing.network:
                        # Standard network backing
                        network_name = device.backing.network.name
                    elif hasattr(device.backing, 'port') and device.backing.port:
                        # Distributed virtual port backing - get the actual portgroup name
                        port = device.backing.port
                        if hasattr(port, 'portgroupKey'):
                            # Resolve the portgroup key to its name by searching all DVS portgroups
                            try:
                                container = self.view_manager.CreateContainerView(
                                    self.content.rootFolder, [vim.dvs.DistributedVirtualPortgroup], True
                                )
                                for pg in container.view:
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

                result_vm_info["network_interfaces"].append({
                    "name": device.deviceInfo.label if device.deviceInfo else "Unknown",
                    "macAddress": device.macAddress,
                    "network": {
                        "name": network_name
                    },
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
                if vimtype == [vim.Datastore] and hasattr(obj, '_moId') and obj._moId == name:
                    return obj

            raise ValueError(f"Object of type {vimtype} with name '{name}' not found.")

        finally:
            container.Destroy()

    def clone_vm(
        self,
        source_vm_name: str,
        clone_vm_name: str,
        session_uuid: str,
        power_on: bool = False,
        regenerate_mac: bool = True,
        target_datastore_id: str = None,
    ) -> vim.VirtualMachine:
        """
        Clones a VM from a source VM or template.

        Args:
            source_vm_name: The name of the VM or template to clone from.
            clone_vm_name: The name of the new VM to be created.
            power_on: Whether to power on the VM after cloning.
            regenerate_mac: Whether to regenerate MAC addresses for network interfaces.
                          Prevents MAC address conflicts between cloned VMs. Default: True.
        """
        clone_vm_name = f"{session_uuid}-{clone_vm_name}"
        LOGGER.info(f"Starting clone process for '{clone_vm_name}' from '{source_vm_name}'")

        source_vm = self.get_obj([vim.VirtualMachine], source_vm_name)

        relocate_spec = vim.vm.RelocateSpec()
        # Use default resource pool instead of template's resource pool to avoid permission issues
        try:
            # Try to find the "Resources" resource pool (default in most vSphere environments)
            resource_pool = self.get_obj([vim.ResourcePool], "Resources")
            relocate_spec.pool = resource_pool
        except ValueError:
            # Fallback to the template's resource pool if "Resources" not found
            LOGGER.warning("Could not find 'Resources' resource pool, using template's resource pool")
            relocate_spec.pool = source_vm.resourcePool

        # Use target datastore if specified, otherwise relay on vsphere's default behaviour
        if target_datastore_id:
            target_datastore = self.get_obj([vim.Datastore], target_datastore_id)
            relocate_spec.datastore = target_datastore
            LOGGER.info(f"Using target datastore: {target_datastore_id}")

        clone_spec = vim.vm.CloneSpec()
        clone_spec.location = relocate_spec
        clone_spec.powerOn = power_on
        clone_spec.template = False

        # Configure MAC address regeneration if requested
        # Note: Skip MAC regeneration during cloning to avoid distributed virtual switch port conflicts
        # MAC addresses will be automatically generated for the new VM
        if regenerate_mac:
            LOGGER.info("MAC regeneration requested but skipping during clone to avoid port conflicts")
            LOGGER.info("New VM will get fresh MAC addresses automatically")

        task = source_vm.CloneVM_Task(folder=source_vm.parent, name=clone_vm_name, spec=clone_spec)
        LOGGER.info(f"Clone task started for {clone_vm_name}. Waiting for completion...")

        res = self.wait_task(
            task=task, action_name=f"Cloning VM {clone_vm_name} from {source_vm_name}", wait_timeout=60 * 20, sleep=5
        )
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
