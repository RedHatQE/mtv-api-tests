from __future__ import annotations

import copy
from typing import Any, Self

from ocp_resources.provider import Provider
from openstack.compute.v2.server import Server as OSP_Server
from openstack.connection import Connection
from openstack.image.v2.image import Image as OSP_Image
from simple_logger.logger import get_logger

from exceptions.exceptions import VmNotFoundError
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
        ocp_resource: Provider | None = None,
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

    def connect(self) -> Self:
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

    def get_volume_metadata(self, vm_name: str) -> Any:
        # Get metadata of the volume attached to the specific instance ID
        instance_id = self.get_instance_id_by_name(name_filter=vm_name)
        # Get the volume attachments associated with the instance
        volume_attachments = self.api.compute.volume_attachments(server=self.api.compute.get_server(instance_id))
        for attachment in volume_attachments:
            volume = self.api.block_storage.get_volume(attachment["volumeId"])
            return volume.volume_image_metadata

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        base_vm_name = kwargs["name"]
        vm_name: str = f"{base_vm_name}{kwargs.get('vm_name_suffix', '')}"

        source_vm = self.get_instance_obj(vm_name)

        if not source_vm and kwargs.get("clone"):
            source_vm = self.clone_vm(
                source_vm_name=base_vm_name, clone_vm_name=vm_name, session_uuid=kwargs["session_uuid"]
            )

        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = "openstack"
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = source_vm.name

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

    def clone_vm(
        self,
        source_vm_name: str,
        clone_vm_name: str,
        session_uuid: str,
        power_on: bool = False,
    ) -> OSP_Server:
        """
        Clones a VM, always reusing the flavor and network from the source.

        Args:
            source_vm_name: The name of the VM to clone.
            clone_vm_name: The name for the new cloned VM.
            power_on: If True, the new VM will be left running. If False,
                      it will be created and then shut off.

        Returns:
            The new server object if successful
        """
        clone_vm_name = f"{session_uuid}-{clone_vm_name}"
        LOGGER.info(f"Starting clone of '{source_vm_name}' to '{clone_vm_name}'")
        source_vm: OSP_Server | None = self.get_instance_obj(name_filter=source_vm_name)

        if not source_vm:
            raise VmNotFoundError(f"Source VM '{source_vm_name}' not found.")

        flavor_id: str = source_vm.flavor["id"]
        networks: list[dict[str, Any]] = self.vm_networks_details(vm_name=source_vm_name)

        if not networks:
            raise ValueError(f"Could not find a network for source VM '{source_vm_name}'.")

        network_id: str = networks[0]["net_id"]
        LOGGER.info(f"Using source flavor '{flavor_id}' and network '{network_id}'")

        snapshot: OSP_Image | None = None

        try:
            snapshot_name = f"{clone_vm_name}-snapshot"
            LOGGER.info(f"Creating snapshot '{snapshot_name}'...")
            snapshot = self.api.compute.create_server_image(server=source_vm.id, name=snapshot_name, wait=True)

            if not snapshot:
                raise Exception("Could not create snapshot.")

            LOGGER.info(f"Creating new server '{clone_vm_name}' from snapshot...")
            new_server: OSP_Server = self.api.compute.create_server(
                name=clone_vm_name,
                image_id=snapshot.id,
                flavor_id=flavor_id,
                networks=[{"uuid": network_id}],
            )
            new_server = self.api.compute.wait_for_server(new_server)

            if not power_on:
                LOGGER.info(f"power_on is False, stopping server '{new_server.name}'")
                self.api.compute.stop_server(new_server)
                new_server = self.api.compute.wait_for_server(new_server, status="SHUTOFF")

            LOGGER.info(f"Successfully cloned '{source_vm_name}' to '{clone_vm_name}'")
            return new_server

        finally:
            if snapshot:
                LOGGER.info(f"Cleaning up snapshot '{snapshot.name}'...")
                self.api.image.delete_image(snapshot.id, ignore_missing=True)

    def delete_vm(self, vm_name: str) -> None:
        """
        Finds and deletes a VM instance.

        Args:
            vm_name: The name of the VM to delete.
        """
        LOGGER.info(f"Attempting to delete VM '{vm_name}'")
        vm_to_delete: OSP_Server | None = self.get_instance_obj(name_filter=vm_name)

        if not vm_to_delete:
            LOGGER.warning(f"VM '{vm_name}' not found. Nothing to delete.")
            return

        try:
            self.api.compute.delete_server(vm_to_delete, wait=True, timeout=180)
            LOGGER.info(f"Successfully deleted VM '{vm_name}'.")
        except Exception as e:
            LOGGER.error(f"An error occurred while deleting VM '{vm_name}': {e}")
