from __future__ import annotations

import copy
import re
from typing import Any

import requests
from ocp_resources.provider import Provider
from ocp_resources.resource import Resource
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import NoVmsFoundError, VmBadDatastoreError, VmMissingVmxError
from libs.base_provider import BaseProvider

LOGGER = get_logger(__name__)


class VMWareProvider(BaseProvider):
    """
    https://github.com/vmware/vsphere-automation-sdk-python
    """

    def __init__(self, host: str, username: str, password: str, ocp_resource: Provider, **kwargs: Any) -> None:
        super().__init__(ocp_resource=ocp_resource, host=host, username=username, password=password, **kwargs)
        if not self.provider_data:
            raise ValueError("provider_data is required, but not provided")

        self.type = Provider.ProviderType.VSPHERE
        self.host = host
        self.username = username
        self.password = password

    def disconnect(self) -> None:
        LOGGER.info(f"Disconnecting VMWareProvider source provider {self.host}")
        Disconnect(si=self.api)

    def connect(self) -> None:
        self.api = SmartConnect(  # ssl cert check is not required
            host=self.host,
            user=self.username,
            pwd=self.password,
            port=443,
            disableSslCertValidation=True,
        )

    @property
    def test(self) -> bool:
        try:
            self.api.RetrieveContent().authorizationManager.description
            return True
        except Exception:
            return False

    @property
    def content(self) -> vim.ServiceInstanceContent:
        return self.api.RetrieveContent()

    def get_view_manager(self) -> vim.view.ViewManager:
        view_manager = self.content.viewManager
        if not view_manager:
            raise ValueError("View manager is not available.")

        return view_manager

    def vms(self, query: str = "") -> list[vim.VirtualMachine]:
        if not self.test:
            LOGGER.info("Reconnecting to VMware")
            self.connect()

        view_manager = self.get_view_manager()
        container_view = view_manager.CreateContainerView(
            container=self.datacenters[0].vmFolder,
            type=[vim.VirtualMachine],
            recursive=True,
        )

        all_vms: list[vim.VirtualMachine] = [vm for vm in container_view.view]  # type:ignore
        container_view.Destroy()

        if not all_vms:
            raise NoVmsFoundError(f"No VMs found at all on host [{self.host}]")

        # Filter VMs based on the query regex if provided
        target_vms = all_vms
        if query:
            pat = re.compile(query, re.IGNORECASE)
            target_vms = [vm for vm in all_vms if pat.search(vm.name)]
            if not target_vms:
                raise NoVmsFoundError(f"No VMs found matching query '{query}' on host [{self.host}]")

        # Perform health checks on the final list of VMs
        vms_with_missing_vmx = [vm.name for vm in target_vms if self.is_vm_missing_vmx_file(vm=vm)]
        if vms_with_missing_vmx:
            raise VmMissingVmxError(vms=vms_with_missing_vmx)

        vms_with_bad_datastore = [vm.name for vm in target_vms if self.is_vm_with_bad_datastore(vm=vm)]
        if vms_with_bad_datastore:
            raise VmBadDatastoreError(vms=vms_with_bad_datastore)

        return target_vms

    @property
    def datacenters(self) -> list[Any]:
        return self.content.rootFolder.childEntity

    def clusters(self, datacenter: str = "") -> list[Any]:
        all_clusters: list[Any] = []

        for dc in self.datacenters:
            clusters = dc.hostFolder.childEntity
            if datacenter:
                if dc.name == datacenter:
                    return clusters

            else:
                all_clusters.extend(clusters)

        return all_clusters

    def cluster(self, name: str, datacenter: str = "") -> Any:
        for cluster in self.clusters(datacenter=datacenter):
            if cluster.name == name:
                return cluster

        return None

    @property
    def storages_name(self):
        """
        Get a list of all data-stores in the cluster
        """
        view_manager = self.get_view_manager()

        return [
            cont_obj.name
            for cont_obj in view_manager.CreateContainerView(
                container=self.content.rootFolder, type=[vim.Datastore], recursive=True
            ).view
        ]

    @property
    def networks_name(self):
        """
        Get a list of all networks in the cluster
        """
        view_manager = self.get_view_manager()
        return [
            cont_obj.name
            for cont_obj in view_manager.CreateContainerView(
                container=self.content.rootFolder, type=[vim.Network], recursive=True
            ).view
        ]

    @property
    def all_storage(self):
        view_manager = self.get_view_manager()
        return [
            {"name": cont_obj.name, "id": str(cont_obj.summary.datastore).split(":")[1]}
            for cont_obj in view_manager.CreateContainerView(
                container=self.content.rootFolder, type=[vim.Datastore], recursive=True
            ).view
        ]

    @property
    def all_networks(self):
        view_manager = self.get_view_manager()
        return [
            {"name": cont_obj.name, "id": cont_obj.summary.network.split(":")[1]}
            for cont_obj in view_manager.CreateContainerView(
                container=self.content.rootFolder, type=[vim.Network], recursive=True
            ).view
        ]

    def wait_task(self, task: vim.Task, action_name: str, wait_timeout: int = 60, sleep: int = 2) -> Any:
        """
        Waits and provides updates on a vSphere task.
        """
        try:
            for sample in TimeoutSampler(
                wait_timeout=wait_timeout,
                sleep=sleep,
                func=lambda: task.info.state == vim.TaskInfo.State.success,
            ):
                if sample:
                    self.log.info(
                        msg=(
                            f"{action_name} completed successfully. "
                            f"{f'result: {task.info.result}' if task.info.result else ''}"
                        )
                    )
                    return task.info.result

                LOGGER.info(f"{action_name} progress: {task.info.progress}%")
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

    def upload_file_to_guest_vm(self, vm, vm_user, vm_password, local_file_path, vm_file_path):
        creds = vim.vm.guest.NamePasswordAuthentication(username=vm_user, password=vm_password)
        with open(local_file_path, "rb") as myfile:
            data_to_send = myfile.read()

        try:
            file_attribute = vim.vm.guest.FileManager.FileAttributes()
            url = self.content.guestOperationsManager.fileManager.InitiateFileTransferToGuest(
                vm, creds, vm_file_path, file_attribute, len(data_to_send), True
            )
            # When : host argument becomes https://*:443/guestFile?
            # Ref: https://github.com/vmware/pyvmomi/blob/master/docs/ \
            #            vim/vm/guest/FileManager.rst
            # Script fails in that case, saying URL has an invalid label.
            # By having hostname in place will take take care of this.
            url = re.sub(r"^https://\*:", "https://" + self.host + ":", url)
            resp = requests.put(url, data=data_to_send, verify=False)
            if not resp.status_code == 200:
                print("Error while uploading file")
            else:
                print("Successfully uploaded file")
        except IOError as ex:
            print(ex)

    def download_file_from_guest_vm(self, vm, vm_user, vm_password, vm_file_path):
        creds = vim.vm.guest.NamePasswordAuthentication(username=vm_user, password=vm_password)

        try:
            _ = vim.vm.guest.FileManager.FileAttributes()
            url = self.content.guestOperationsManager.fileManager.InitiateFileTransferFromGuest(
                vm, creds, vm_file_path
            ).url
            # When : host argument becomes https://*:443/guestFile?
            # Ref: https://github.com/vmware/pyvmomi/blob/master/docs/ \
            #            vim/vm/guest/FileManager.rst
            # Script fails in that case, saying URL has an invalid label.
            # By having hostname in place will take take care of this.
            url = re.sub(r"^https://\*:", "https://" + self.host + ":", url)
            resp = requests.get(url, verify=False)
            if not resp.status_code == 200:
                print("Error while downloading file")
            else:
                print("Successfully downloaded file")
                return resp.content.decode("utf-8")
        except IOError as ex:
            print(ex)

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        vm_name = kwargs["name"]
        source_vm = self.vms(query=f"^{vm_name}$")[0]
        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = Resource.ProviderType.VSPHERE
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = vm_name
        __import__("ipdb").set_trace()

        vm_config: Any = source_vm.config
        if not vm_config:
            raise ValueError(f"No config found for VM {vm_name}")

        # Devices
        for device in vm_config.hardware.device:
            # Network Interfaces
            if isinstance(device, vim.vm.device.VirtualEthernetCard):
                result_vm_info["network_interfaces"].append({
                    "name": device.deviceInfo.label,
                    "macAddress": device.macAddress,
                    "network": {"name": device.backing.network.name},
                })

            # Disks
            if isinstance(device, vim.vm.device.VirtualDisk):
                result_vm_info["disks"].append({
                    "name": device.deviceInfo.label,
                    "size_in_kb": device.capacityInKB,
                    "storage": dict(name=device.backing.datastore.name),
                })

        # CPUs
        result_vm_info["cpu"]["num_cores"] = vm_config.hardware.numCoresPerSocket
        result_vm_info["cpu"]["num_sockets"] = int(vm_config.hardware.numCPU / result_vm_info["cpu"]["num_cores"])

        # Memory
        result_vm_info["memory_in_mb"] = vm_config.hardware.memoryMB

        # Snapshots details
        for snapshot in self.list_snapshots(source_vm):
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
            hasattr(source_vm, "runtime")
            and source_vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn
            and source_vm.guest.toolsStatus == vim.vm.GuestInfo.ToolsStatus.toolsOk
        )

        # Guest OS
        result_vm_info["win_os"] = "win" in vm_config.guestId

        # Power state
        if source_vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
            result_vm_info["power_state"] = "on"
        elif source_vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff:
            result_vm_info["power_state"] = "off"
        else:
            result_vm_info["power_state"] = "other"

        return result_vm_info

    def upload_data_to_vms(self, vm_names_list):
        for vm_name in vm_names_list:
            vm_dict = self.vm_dict(name=vm_name)
            vm = vm_dict["provider_vm_api"]
            if "linux" in vm.guest.guestFamily:
                guest_vm_file_path = "/tmp/mtv-api-test"
                guest_vm_user = self.provider_data["guest_vm_linux_user"]
                guest_vm_password = self.provider_data["guest_vm_linux_password"]
            else:
                guest_vm_file_path = "c:\\mtv-api-test.txt"
                guest_vm_user = self.provider_data["guest_vm_linux_user"]
                guest_vm_password = self.provider_data["guest_vm_linux_user"]

            local_data_file_path = "/tmp/data.mtv"

            current_file_content = self.download_file_from_guest_vm(
                vm=vm, vm_file_path=guest_vm_file_path, vm_user=guest_vm_user, vm_password=guest_vm_password
            )
            if not current_file_content or not vm_dict["guest_agent_running"]:
                vm_names_list.remove(vm_name)
                continue

            prev_number_of_snapshots = current_file_content.split("|")[-1]
            current_number_of_snapshots = str(len(vm_dict["snapshots_data"]))

            if prev_number_of_snapshots != current_number_of_snapshots:
                new_data_content = f"{current_file_content}|{current_number_of_snapshots}"

                with open(local_data_file_path, "w") as local_data_file:
                    local_data_file.write(new_data_content)

                self.upload_file_to_guest_vm(
                    vm=vm,
                    vm_file_path=guest_vm_file_path,
                    local_file_path=local_data_file_path,
                    vm_user=guest_vm_user,
                    vm_password=guest_vm_password,
                )
        return vm_names_list

    def clear_vm_data(self, vm_names_list):
        for vm_name in vm_names_list:
            vm_dict = self.vm_dict(name=vm_name)
            vm = vm_dict["provider_vm_api"]
            if "linux" in vm.guest.guestFamily:
                guest_vm_file_path = "/tmp/mtv-api-test"
                guest_vm_user = self.provider_data["guest_vm_linux_user"]
                guest_vm_password = self.provider_data["guest_vm_linux_password"]
            else:
                guest_vm_file_path = "c:\\mtv-api-test.txt"
                guest_vm_user = self.provider_data["guest_vm_linux_user"]
                guest_vm_password = self.provider_data["guest_vm_linux_user"]

            local_data_file_path = "/tmp/data.mtv"

            with open(local_data_file_path, "w") as local_data_file:
                local_data_file.write("|-1")

            self.upload_file_to_guest_vm(
                vm=vm,
                vm_file_path=guest_vm_file_path,
                local_file_path=local_data_file_path,
                vm_user=guest_vm_user,
                vm_password=guest_vm_password,
            )

    def wait_for_snapshots(self, vm_names_list, number_of_snapshots):
        """
        return when all vms in the list have a min number of snapshots.
        """
        while vm_names_list:
            for vm_name in vm_names_list:
                if len(self.vm_dict(name=vm_name)["snapshots_data"]) >= number_of_snapshots:
                    vm_names_list.remove(vm_name)

    def is_vm_missing_vmx_file(self, vm: vim.VirtualMachine) -> bool:
        if not vm.datastore:
            self.log.error(f"VM {vm.name} is inaccessible due to datastore error")
            return True

        vm_datastore_info = vm.datastore[0].browser.Search(vm.config.files.vmPathName)
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

    def get_obj(self, vimtype, name):
        container = self.content.viewManager.CreateContainerView(self.content.rootFolder, vimtype, True)
        for obj in container.view:
            if obj.name == name:
                container.Destroy()
                return obj

        container.Destroy()
        raise ValueError(f"Object of type {vimtype} with name '{name}' not found.")

    def clone_vm(
        self,
        source_vm_name: str,
        clone_vm_name: str,
        power_on: bool = False,
    ) -> vim.VirtualMachine:
        """
        Clones a VM from a source VM or template.

        Args:
            source_vm_name: The name of the VM or template to clone from.
            new_vm_name: The name of the new VM to be created.
            power_on: Whether to power on the VM after cloning.
        """
        LOGGER.info(f"Starting clone process for '{clone_vm_name}' from '{source_vm_name}'")

        source_vm = self.get_obj([vim.VirtualMachine], source_vm_name)

        relocate_spec = vim.vm.RelocateSpec()
        relocate_spec.pool = source_vm.resourcePool
        relocate_spec.datastore = source_vm.datastore[0]

        clone_spec = vim.vm.CloneSpec()
        clone_spec.location = relocate_spec
        clone_spec.powerOn = power_on
        clone_spec.template = False

        task = source_vm.CloneVM_Task(folder=source_vm.parent, name=clone_vm_name, spec=clone_spec)
        LOGGER.info(f"Clone task started for {clone_vm_name}. Waiting for completion...")

        return self.wait_task(task=task, action_name=f"Cloning VM {source_vm_name}", wait_timeout=60 * 10, sleep=5)
