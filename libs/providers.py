import abc
import re

import ovirtsdk4
import openstack
import glanceclient.v2.client as glclient

from ovirtsdk4.types import VmStatus
from ovirtsdk4 import NotFoundError
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutSampler, TimeoutExpiredError
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim
import requests


class MissingResourceError(Exception):
    pass


class Provider(abc.ABC):
    def __init__(
        self,
        username,
        password,
        host,
        debug=False,
        log=None,
    ):
        self.username = username
        self.password = password
        self.host = host
        self.debug = debug
        self.log = log or get_logger(name=__name__)
        self.api = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return

    @abc.abstractmethod
    def connect(self):
        pass

    @abc.abstractmethod
    def disconnect(self):
        pass

    @abc.abstractmethod
    def test(self):
        pass


class RHV(Provider):
    """
    https://github.com/oVirt/ovirt-engine-sdk/tree/master/sdk/examples
    """

    def __init__(
        self,
        host,
        username,
        password,
        ca_file,
        debug=False,
        log=None,
        insecure=False,
    ):
        super().__init__(
            host=host,
            username=username,
            password=password,
            debug=debug,
            log=log,
        )
        self.insecure = insecure
        self.ca_file = ca_file

    def disconnect(self):
        self.api.close()

    def connect(self):
        self.api = ovirtsdk4.Connection(
            url=self.host,
            username=self.username,
            password=self.password,
            ca_file=self.ca_file,
            debug=self.debug,
            log=self.log,
            insecure=self.insecure,
        )
        return self

    @property
    def test(self):
        return self.api.test()

    @property
    def vms_services(self):
        return self.api.system_service().vms_service()

    @property
    def disks_service(self):
        return self.api.system_service().disks_service()

    @property
    def network_services(self):
        return self.api.system_service().networks_service()

    @property
    def storage_services(self):
        return self.api.system_service().storage_domains_service()

    def events_service(self):
        return self.api.system_service().events_service()

    def events_list_by_vm(self, vm):
        return self.events_service().list(search=f"Vms.id = {vm.id}")

    def vms(self, search):
        return self.vms_services.list(search=search)

    def vm(self, name, cluster=None):
        query = f"name={name}"
        if cluster:
            query = f"{query} cluster={cluster}"

        return self.vms(search=query)[0]

    def vm_nics(self, vm):
        return [self.api.follow_link(nic) for nic in self.vms_services.vm_service(id=vm.id).nics_service().list()]

    def vm_disk_attachments(self, vm):
        return [
            self.api.follow_link(disk.disk)
            for disk in self.vms_services.vm_service(id=vm.id).disk_attachments_service().list()
        ]

    def list_snapshots(self, vm):
        snapshots = []
        for snapshot in self.vms_services.vm_service(id=vm.id).snapshots_service().list():
            try:
                _snapshot = self.api.follow_link(snapshot)
                snapshots.append(_snapshot)
            except NotFoundError:
                continue
        return snapshots

    def start_vm(self, vm):
        if vm.status != VmStatus.UP:
            self.vms_services.vm_service(vm.id).start()

    # TODO: change the function definition to shutdown_vm once we will have the same for VMware
    def power_off_vm(self, vm):
        if vm.status == VmStatus.UP:
            self.vms_services.vm_service(vm.id).shutdown()

    @property
    def networks_name(self):
        return [f"{network.name}/{network.name}" for network in self.network_services.list()]

    @property
    def networks_id(self):
        return [network.id for network in self.network_services.list()]

    @property
    def networks(self):
        return [
            {"name": network.name, "id": network.id, "data_center": self.api.follow_link(network.data_center).name}
            for network in self.network_services.list()
        ]

    @property
    def storages_name(self):
        return [storage.name for storage in self.storage_services.list()]

    @property
    def storage_groups(self):
        return [{"name": storage.name, "id": storage.id} for storage in self.storage_services.list()]


class VMWare(Provider):
    """
    https://github.com/vmware/vsphere-automation-sdk-python
    """

    def __init__(
        self,
        host,
        username,
        password,
        debug=False,
        log=None,
    ):
        super().__init__(
            host=host,
            username=username,
            password=password,
            debug=debug,
            log=log,
        )

    def disconnect(self):
        Disconnect(si=self.api)

    def connect(self):
        self.api = SmartConnect(  # ssl cert check is not required
            host=self.host,
            user=self.username,
            pwd=self.password,
            port=443,
            disableSslCertValidation=True,
        )

    def test(self):
        return not self.api

    @property
    def content(self):
        return self.api.RetrieveContent()

    def vms(self, search=None, folder=None):
        if folder:
            vms = [
                __vm
                for __vm in [
                    _vm
                    for _vm in self.content.rootFolder.childEntity[0].vmFolder.childEntity
                    if (isinstance(_vm, vim.Folder)) and _vm.name == folder
                ][0].childEntity
                if isinstance(__vm, vim.VirtualMachine)
            ]
        else:
            container = self.content.rootFolder.childEntity[0].vmFolder  # starting point to look into
            view_type = [vim.VirtualMachine]  # object types to look for
            recursive = True  # whether we should look into it recursively
            container_view = self.content.viewManager.CreateContainerView(container, view_type, recursive)
            vms = container_view.view

        result = []
        if not search:
            return vms

        pat = re.compile(search, re.IGNORECASE)
        for vm in vms:
            if pat.search(vm.name) is not None:
                result.append(vm)
        return result

    def vm(self, name, datacenter=None, cluster=None):
        if cluster:
            _cluster = self.cluster(name=cluster, datacenter=datacenter)
            for host in _cluster.host:
                for vm in host.vm:
                    if vm.summary.config.name == name:
                        return vm

        return self.vms(search=name)

    def vm_by_id(self, vm_id):
        return [vm for vm in self.vms() if str(vm).split(":")[1][:-1] == vm_id][0]

    @property
    def datacenters(self):
        return self.content.rootFolder.childEntity

    def clusters(self, datacenter=None):
        all_clusters = []
        for dc in self.datacenters:  # Iterate though DataCenters
            clusters = dc.hostFolder.childEntity
            if dc.name == datacenter:
                return clusters

            if datacenter:
                continue

            for cluster in clusters:  # Iterate through the clusters in the DC
                all_clusters.append(cluster)

        return all_clusters

    def cluster(self, name, datacenter=None):
        for cluster in self.clusters(datacenter=datacenter):
            if cluster.name == name:
                return cluster

    def get_resource_obj(self, resource_type, resource_name):
        """
        Get the vsphere resource object associated with a given resource_name.
        """
        containers = self.content.viewManager.CreateContainerView(
            container=self.content.rootFolder, type=resource_type, recursive=True
        )
        for cont_obj in containers.view:
            if cont_obj.name == resource_name:
                return cont_obj

        raise MissingResourceError(f"{resource_type}: {resource_name}")

    @property
    def storages_name(self):
        """
        Get a list of all data-stores in the cluster
        """
        return [
            cont_obj.name
            for cont_obj in self.content.viewManager.CreateContainerView(
                container=self.content.rootFolder, type=[vim.Datastore], recursive=True
            ).view
        ]

    @property
    def networks_name(self):
        """
        Get a list of all networks in the cluster
        """
        return [
            cont_obj.name
            for cont_obj in self.content.viewManager.CreateContainerView(
                container=self.content.rootFolder, type=[vim.Network], recursive=True
            ).view
        ]

    @property
    def all_storage(self):
        return [
            {"name": cont_obj.name, "id": str(cont_obj.summary.datastore).split(":")[1]}
            for cont_obj in self.content.viewManager.CreateContainerView(
                container=self.content.rootFolder, type=[vim.Datastore], recursive=True
            ).view
        ]

    @property
    def all_networks(self):
        return [
            {"name": cont_obj.name, "id": cont_obj.summary.network.split(":")[1]}
            for cont_obj in self.content.viewManager.CreateContainerView(
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


class OpenStack(Provider):
    """
    https://docs.openstack.org/openstacksdk/latest/user/guides/compute.html
    """

    def __init__(
        self,
        host,
        username,
        password,
        auth_url,
        project_name,
        user_domain_name,
        region_name,
        user_domain_id,
        project_domain_id,
        debug=False,
        log=None,
        insecure=False,
    ):
        super().__init__(
            host=host,
            username=username,
            password=password,
            debug=debug,
            log=log,
        )
        self.insecure = insecure
        self.auth_url = auth_url
        self.project_name = project_name
        self.user_domain_name = user_domain_name
        self.region_name = region_name
        self.user_domain_id = user_domain_id
        self.project_domain_id = project_domain_id

    def disconnect(self):
        self.api.close()

    def connect(self):
        self.api = openstack.connection.Connection(
            auth_url=self.auth_url,
            project_name=self.project_name,
            username=self.username,
            password=self.password,
            user_domain_name=self.user_domain_name,
            region_name=self.region_name,
            user_domain_id=self.user_domain_id,
            project_domain_id=self.project_domain_id,
        )
        return self

    @property
    def test(self):
        return True

    @property
    def networks(self):
        return self.api.network.networks()

    @property
    def storages_name(self):
        return [storage.name for storage in self.api.search_volume_types()]

    @property
    def vms_list(self):
        instances = self.api.compute.servers()
        return [vm.name for vm in instances]

    def get_instance_id_by_name(self, name_filter):
        # Retrieve the specific instance ID
        instance_id = None
        for server in self.api.compute.servers(details=True):
            if server.name == name_filter:
                instance_id = server.id
                break
        return instance_id

    def get_instance_obj(self, name_filter):
        instance_id = self.get_instance_id_by_name(name_filter=name_filter)
        if instance_id:
            return self.api.compute.get_server(instance_id)

    def list_snapshots(self, vm_name):
        # Get list of snapshots for future use.
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        if instance_id:
            volumes = self.api.block_storage.volumes(details=True, attach_to=instance_id)
            return [list(self.api.block_storage.snapshots(volume_id=volume.id)) for volume in volumes]

    def list_network_interfaces(self, vm_name):
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        if instance_id:
            return [port for port in self.api.network.ports(device_id=instance_id)]

    def vm_networks_details(self, vm_name):
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        vm_networks_details = [
            {"net_name": network.name, "net_id": network.id}
            for port in self.api.network.ports(device_id=instance_id)
            if (network := self.api.network.get_network(port.network_id))
        ]
        return vm_networks_details

    def list_volumes(self, vm_name):
        return [
            self.api.block_storage.get_volume(attachment["volumeId"])
            for attachment in self.api.compute.volume_attachments(server=self.get_instance_obj(name_filter=vm_name))
        ]

    def get_flavor_obj(self, vm_name):
        # Retrieve the specific instance
        instance_obj = self.get_instance_obj(name_filter=vm_name)
        return next(
            (flavor for flavor in self.api.compute.flavors() if flavor.name == instance_obj.flavor.original_name), None
        )

    def get_image_obj(self, vm_name):
        # Get custom image object built on the base of the instance.
        # For Openstack migration the instance is created by booting from a volume instead of an image.
        # In this case, we can't see an image associated with the instance as the part of the instance object.
        # To get the attributes of the image we use custom image created in advance on the base of the instance.
        glance_connect = glclient.Client(
            session=self.api.session,
            endpoint=self.api.session.get_endpoint(service_type="image"),
            interface="public",
            region_name=self.region_name,
        )
        images = [image for image in glance_connect.images.list() if vm_name in image.get("name")]
        return images[0] if images else None

    def get_volume_metadata(self, vm_name):
        # Get metadata of the volume attached to the specific instance ID
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        # Get the volume attachments associated with the instance
        volume_attachments = self.api.compute.volume_attachments(server=self.api.compute.get_server(instance_id))
        for attachment in volume_attachments:
            volume = self.api.block_storage.get_volume(attachment["volumeId"])
            return volume.volume_image_metadata


class OVA(Provider):
    """ """

    def __init__(
        self,
        host,
        username,
        password,
        debug=False,
        log=None,
    ):
        super().__init__(
            host=host,
            username=username,
            password=password,
            debug=debug,
            log=log,
        )

    def disconnect(self):
        return True

    def connect(self):
        return True

    @property
    def test(self):
        return True
