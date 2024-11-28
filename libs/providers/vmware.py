import copy
from typing import Any

from ocp_resources.exceptions import MissingResourceResError
from timeout_sampler import TimeoutSampler, TimeoutExpiredError

from ocp_resources.mtv import MTV
from pyVmomi import vim

import re


from pyVim.connect import Disconnect, SmartConnect
import requests

from libs.base_provider import BaseProvider


class VMWareProvider(BaseProvider):
    """
    https://github.com/vmware/vsphere-automation-sdk-python
    """

    def __init__(self, host: str, username: str, password: str, **kwargs: Any) -> None:
        super().__init__(host, username, password, **kwargs)
        self.host = host
        self.username = username
        self.password = password

    def disconnect(self) -> None:
        Disconnect(si=self.api)

    def connect(self) -> None:
        self.api = SmartConnect(  # ssl cert check is not required
            host=self.host,
            user=self.username,
            pwd=self.password,
            port=443,
            disableSslCertValidation=True,
        )

    def test(self) -> bool:
        return bool(self.api)

    @property
    def content(self) -> vim.ServiceInstanceContent:
        return self.api.RetrieveContent()

    def get_view_manager(self) -> vim.view.ViewManager:
        view_manager = self.content.viewManager
        if not view_manager:
            raise ValueError("View manager is not available.")

        return view_manager

    def vms(self, query: str = "") -> list[Any]:
        view_manager = self.get_view_manager()

        container_view = view_manager.CreateContainerView(
            container=self.datacenters[0].vmFolder, type=[vim.VirtualMachine], recursive=True
        )
        vms = [vm for vm in container_view.view]  # type: ignore

        result = []
        if not query:
            return vms

        pat = re.compile(query, re.IGNORECASE)
        for vm in vms:
            if pat.search(vm.name) is not None:
                result.append(vm)

        return result

    @property
    def datacenters(self) -> list[Any]:
        return self.content.rootFolder.childEntity

    def clusters(self, datacenter: str = "") -> list[Any]:
        all_clusters: list[Any] = []

        for dc in self.datacenters:  # Iterate though DataCenters
            clusters = dc.hostFolder.childEntity
            if datacenter:
                if dc.name == datacenter:
                    return clusters

            else:
                for cluster in clusters:  # Iterate through the clusters in the DC
                    all_clusters.append(cluster)

        return all_clusters

    def cluster(self, name: str, datacenter: str = "") -> Any:
        for cluster in self.clusters(datacenter=datacenter):
            if cluster.name == name:
                return cluster

        return None

    def get_resource_obj(self, resource_type, resource_name):
        """
        Get the vsphere resource object associated with a given resource_name.
        """
        view_manager = self.get_view_manager()

        containers = view_manager.CreateContainerView(
            container=self.content.rootFolder, type=resource_type, recursive=True
        )
        for cont_obj in containers.view:
            if cont_obj.name == resource_name:
                return cont_obj

        raise MissingResourceResError(f"{resource_type}: {resource_name}")

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

    def wait_task(self, task, action_name="job"):
        """
        Waits and provides updates on a vSphere task.
        """
        try:
            for sample in TimeoutSampler(
                wait_timeout=60,
                sleep=2,
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
        except TimeoutExpiredError:
            self.log.error(msg=f"{action_name} did not complete successfully: {task.info.error}")
            raise

    def get_vm_clone_spec(self, cluster_name, power_on, vm_flavor, datastore_name):
        cluster = self.cluster(name=cluster_name)
        resource_pool = cluster.resourcePool
        # Relocation spec
        relospec = vim.vm.RelocateSpec()
        relospec.pool = resource_pool

        if datastore_name:
            data_store = self.get_resource_obj(
                resource_type=[vim.Datastore],
                resource_name=datastore_name,
            )
            relospec.datastore = data_store

        vmconf = vim.vm.ConfigSpec()
        if vm_flavor:
            # VM config spec
            vmconf.numCPUs = vm_flavor["cpus"]
            vmconf.memoryMB = vm_flavor["memory"]
            vmconf.changeTrackingEnabled = vm_flavor["cbt_enabled"]

        clone_spec = vim.vm.CloneSpec(
            powerOn=power_on,
            template=False,
            location=relospec,
            customization=None,
            config=vmconf,
        )

        return clone_spec

    def clone_vm_from_template(
        self,
        cluster_name,
        template_name,
        vm_name,
        power_on=True,
        vm_flavor=None,
        datastore_name=None,
    ):
        """
        Create a new vm by cloning the template provided using template_name.
        By default it uses the spec of the template to create new vm.
        vm_flavor and datastore_name can be changed if required.
        vm_flavor (dict): {'cpu': <number of vCPU>, 'memory':<RAM size in MB>}
        datastore_name (str): '<new datastore name>'
        """
        template_vm = self.get_resource_obj(
            resource_type=[vim.VirtualMachine],
            resource_name=template_name,
        )
        clone_spec = self.get_vm_clone_spec(
            cluster_name=cluster_name,
            power_on=power_on,
            vm_flavor=vm_flavor,
            datastore_name=datastore_name,
        )
        # Creating clone task
        task = template_vm.Clone(name=vm_name, folder=template_vm.parent, spec=clone_spec)

        return self.wait_task(task=task, action_name="VM clone task")

    def start_vm(self, vm):
        if vm.runtime.powerState != vm.runtime.powerState.poweredOn:
            self.wait_task(task=vm.PowerOn())

    def power_off_vm(self, vm):
        if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
            self.wait_task(task=vm.PowerOff())

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

    def vm_dict(self, **xargs):
        vm_name = xargs["name"]
        source_vm = self.vms(query=f"^{vm_name}$")[0]
        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = MTV.ProviderType.VSPHERE
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = xargs["name"]

        # Devices
        for device in source_vm.config.hardware.device:
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
        result_vm_info["cpu"]["num_cores"] = source_vm.config.hardware.numCoresPerSocket
        result_vm_info["cpu"]["num_sockets"] = int(
            source_vm.config.hardware.numCPU / result_vm_info["cpu"]["num_cores"]
        )

        # Memory
        result_vm_info["memory_in_mb"] = source_vm.config.hardware.memoryMB

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
        result_vm_info["win_os"] = "win" in source_vm.config.guestId

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
