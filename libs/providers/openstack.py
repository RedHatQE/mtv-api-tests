from __future__ import annotations

import copy
from typing import Any

import glanceclient.v2.client as glclient
from ocp_resources.provider import Provider
from openstack.connection import Connection
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider

LOGGER = get_logger(__name__)


class OpenStackProvider(BaseProvider):
    """
    https://docs.openstack.org/openstacksdk/latest/user/guides/compute.html
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        auth_url: str,
        project_name: str,
        user_domain_name: str,
        region_name: str,
        user_domain_id: str,
        project_domain_id: str,
        ocp_resource: Provider,
        insecure: bool = False,
        **kwargs: Any,
    ):
        super().__init__(
            ocp_resource=ocp_resource,
            host=host,
            username=username,
            password=password,
            **kwargs,
        )
        self.type = Provider.ProviderType.OPENSTACK
        self.insecure = insecure
        self.auth_url = auth_url
        self.project_name = project_name
        self.user_domain_name = user_domain_name
        self.region_name = region_name
        self.user_domain_id = user_domain_id
        self.project_domain_id = project_domain_id

    def disconnect(self) -> None:
        LOGGER.info(f"Disconnecting OpenStackProvider source provider {self.host}")
        self.api.close()

    def connect(self) -> "OpenStackProvider":
        self.api = Connection(
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
    def test(self) -> bool:
        return True

    @property
    def networks(self) -> list[Any]:
        return self.api.network.networks()

    @property
    def storages_name(self) -> list[str]:
        return [storage.name for storage in self.api.search_volume_types()]

    @property
    def vms_list(self) -> list[str]:
        instances = self.api.compute.servers()
        return [vm.name for vm in instances]

    def get_instance_id_by_name(self, name_filter: str) -> str:
        # Retrieve the specific instance ID
        instance_id = ""
        for server in self.api.compute.servers(details=True):
            if server.name == name_filter:
                instance_id = server.id
                break
        return instance_id

    def get_instance_obj(self, name_filter: str) -> Any:
        instance_id = self.get_instance_id_by_name(name_filter=name_filter)
        if instance_id:
            return self.api.compute.get_server(instance_id)

    def list_snapshots(self, vm_name: str) -> list[Any]:
        # Get list of snapshots for future use.
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        if instance_id:
            volumes = self.api.block_storage.volumes(details=True, attach_to=instance_id)
            return [list(self.api.block_storage.snapshots(volume_id=volume.id)) for volume in volumes]
        return []

    def list_network_interfaces(self, vm_name: str) -> list[Any]:
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        if instance_id:
            return [port for port in self.api.network.ports(device_id=instance_id)]
        return []

    def vm_networks_details(self, vm_name: str) -> list[dict[str, Any]]:
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        vm_networks_details = [
            {"net_name": network.name, "net_id": network.id}
            for port in self.api.network.ports(device_id=instance_id)
            if (network := self.api.network.get_network(port.network_id))
        ]
        return vm_networks_details

    def list_volumes(self, vm_name: str) -> list[Any]:
        return [
            self.api.block_storage.get_volume(attachment["volumeId"])
            for attachment in self.api.compute.volume_attachments(server=self.get_instance_obj(name_filter=vm_name))
        ]

    def get_flavor_obj(self, vm_name: str) -> Any:
        # Retrieve the specific instance
        instance_obj = self.get_instance_obj(name_filter=vm_name)
        if not instance_obj:
            LOGGER.error(f"Instance {vm_name} not found.")
            return None

        return next(
            (flavor for flavor in self.api.compute.flavors() if flavor.name == instance_obj.flavor.original_name), None
        )

    def get_image_obj(self, vm_name: str) -> Any:
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

    def get_volume_metadata(self, vm_name: str) -> Any:
        # Get metadata of the volume attached to the specific instance ID
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        # Get the volume attachments associated with the instance
        volume_attachments = self.api.compute.volume_attachments(server=self.api.compute.get_server(instance_id))
        for attachment in volume_attachments:
            volume = self.api.block_storage.get_volume(attachment["volumeId"])
            return volume.volume_image_metadata

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        vm_name: str = kwargs["name"]
        source_vm = self.get_instance_obj(vm_name)
        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = "openstack"
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = kwargs["name"]

        # Snapshots details
        for volume_snapshots in self.list_snapshots(vm_name):
            for snapshot in volume_snapshots:
                result_vm_info["snapshots_data"].append({
                    "description": snapshot.name,
                    "id": snapshot.id,
                    "snapshot_status": snapshot.status,
                })

        # Network Interfaces
        vm_networks_details = self.vm_networks_details(vm_name=vm_name)
        for network, details in zip(self.list_network_interfaces(vm_name=vm_name), vm_networks_details):
            if network.network_id == details["net_id"]:
                result_vm_info["network_interfaces"].append({
                    "name": details["net_name"],
                    "macAddress": network.mac_address,
                    "network": {"name": details["net_name"], "id": network.network_id},
                })

        # Disks
        for disk in self.list_volumes(vm_name=vm_name):
            result_vm_info["disks"].append({
                "name": disk.name,
                "size_in_kb": disk.size,
                "storage": dict(name=disk.availability_zone, id=disk.id),
            })

        # CPUs
        volume_metadata = self.get_volume_metadata(vm_name=vm_name)
        result_vm_info["cpu"]["num_cores"] = int(volume_metadata["hw_cpu_cores"])
        result_vm_info["cpu"]["num_threads"] = int(volume_metadata["hw_cpu_threads"])
        result_vm_info["cpu"]["num_sockets"] = int(volume_metadata["hw_cpu_sockets"])

        # Memory
        flavor = self.get_flavor_obj(vm_name=vm_name)
        result_vm_info["memory_in_mb"] = flavor.ram

        # Power state
        if source_vm.status == "ACTIVE":
            result_vm_info["power_state"] = "on"
        elif source_vm.status == "SHUTOFF":
            result_vm_info["power_state"] = "off"
        else:
            result_vm_info["power_state"] = "other"
        return result_vm_info
