from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Self

from ocp_resources.provider import Provider
from openstack import exceptions as os_exc
from openstack.compute.v2.server import Server as OSP_Server
from openstack.connection import Connection
from openstack.image.v2.image import Image as OSP_Image
from simple_logger.logger import get_logger

from exceptions.exceptions import VmNotFoundError
from libs.base_provider import BaseProvider
from utilities.naming import generate_name_with_uuid

if TYPE_CHECKING:
    from libs.forklift_inventory import ForkliftInventory

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

    def _get_attached_volumes(self, server: OSP_Server) -> list[Any]:
        """Get all volumes attached to a server.

        Args:
            server: OpenStack server object

        Returns:
            List of volume objects attached to the server
        """
        return [
            self.api.block_storage.get_volume(attachment["volumeId"])
            for attachment in self.api.compute.volume_attachments(server=server)
        ]

    def list_snapshots(self, vm_name: str) -> list[list[Any]]:
        """Get snapshots for all volumes attached to a VM.

        Returns a list of snapshot lists - one inner list per volume attached to the VM.
        Each inner list contains all snapshots for that specific volume.

        Args:
            vm_name: Name of the VM instance

        Returns:
            List of snapshot lists. Outer list corresponds to volumes, inner lists contain
            snapshots for each volume. Returns empty list if VM not found.

        Raises:
            OpenStack API exceptions may be raised during volume or snapshot retrieval.

        Note:
            Uses get_instance_obj to retrieve the VM, then iterates through volume_attachments
            to get volumes via block_storage.get_volume, and finally retrieves snapshots for
            each volume via block_storage.snapshots.
        """
        instance_obj = self.get_instance_obj(name_filter=vm_name)
        if instance_obj:
            volumes = self._get_attached_volumes(server=instance_obj)
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
        instance_obj = self.get_instance_obj(name_filter=vm_name)
        return self._get_attached_volumes(server=instance_obj) if instance_obj else []

    def get_flavor_obj(self, vm_name: str) -> Any:
        # Retrieve the specific instance
        instance_obj = self.get_instance_obj(name_filter=vm_name)
        if not instance_obj:
            LOGGER.warning(f"Instance {vm_name} not found.")
            return None

        return next(
            (flavor for flavor in self.api.compute.flavors() if flavor.name == instance_obj.flavor.original_name), None
        )

    def get_volume_metadata(self, vm_name: str) -> Any:
        """Get metadata from the boot volume attached to the VM.

        Args:
            vm_name: Name of the VM instance

        Returns:
            Volume image metadata from the boot volume

        Raises:
            ValueError: If no boot volume is found for the VM

        Note:
            The OpenStack volume attachment API does not expose boot_index.
            This method relies on the volume's is_bootable flag to identify the boot volume.
        """
        instance_obj = self.get_instance_obj(name_filter=vm_name)
        if not instance_obj:
            raise ValueError(f"VM '{vm_name}' not found")

        volumes = self._get_attached_volumes(server=instance_obj)

        for volume in volumes:
            if volume.is_bootable:
                return volume.volume_image_metadata

        LOGGER.error(f"No boot volume found for VM '{vm_name}'. Checked {len(volumes)} volumes.")
        raise ValueError(f"No boot volume found for VM '{vm_name}'")

    def _is_windows(self, os_type_str: str | None) -> bool:
        """Check if OS type indicates Windows.

        Args:
            os_type_str: OS type string from image or volume metadata, or None

        Returns:
            True if OS type indicates Windows
        """
        os_type: str = os_type_str.lower() if os_type_str else ""
        return "windows" in os_type

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        # If provider_vm_api is passed, use it directly (VM already retrieved/cloned)
        source_vm = kwargs.get("provider_vm_api")

        if not source_vm:
            base_vm_name = kwargs["name"]
            source_vm = self.get_vm_by_name(
                query=base_vm_name,
                vm_name_suffix=kwargs.get("vm_name_suffix", ""),
                clone_vm=kwargs.get("clone", False),
                session_uuid=kwargs.get("session_uuid", ""),
                clone_options=kwargs.get("clone_options"),
            )

        vm_name = source_vm.name

        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = "openstack"
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = source_vm.name
        result_vm_info["id"] = source_vm.id  # OpenStack VM/Instance ID (UUID)

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
                "device_key": disk.id,  # OpenStack volume ID
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

        # Guest OS - detect Windows from volume metadata (volume-backed) or image metadata (image-based)
        win_os = False
        if volume_metadata:
            win_os = self._is_windows(volume_metadata.get("os_type", ""))
        elif source_vm.image:
            try:
                image = self.api.image.get_image(source_vm.image["id"])
                win_os = self._is_windows(getattr(image, "os_type", ""))
            except os_exc.SDKException as e:
                LOGGER.warning(f"Failed to get Windows OS info from image for VM '{vm_name}': {e}")
        result_vm_info["win_os"] = win_os

        return result_vm_info

    def clone_vm(
        self,
        source_vm_name: str,
        clone_vm_name: str,
        session_uuid: str,
        power_on: bool = False,
        **kwargs: Any,
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
        clone_vm_name = self._generate_clone_vm_name(session_uuid=session_uuid, base_name=clone_vm_name)
        LOGGER.info(f"Starting clone of '{source_vm_name}' to '{clone_vm_name}'")
        source_vm = self.get_instance_obj(name_filter=source_vm_name)
        if not source_vm:
            raise VmNotFoundError(f"Source VM '{source_vm_name}' not found.")

        # Get the flavor object to retrieve the actual UUID (not the string name)
        flavor_obj = self.get_flavor_obj(vm_name=source_vm_name)
        if not flavor_obj:
            raise ValueError(f"Could not find flavor for source VM '{source_vm_name}'.")
        flavor_id: str = flavor_obj.id

        source_volumes = self._get_attached_volumes(server=source_vm)

        bootable_volumes = [vol for vol in source_volumes if vol.is_bootable]
        if len(bootable_volumes) == 1:
            boot_volume_size = bootable_volumes[0].size
        elif source_vm.image:
            boot_volume_size = flavor_obj.disk
        else:
            raise ValueError(f"Could not determine boot volume size for '{source_vm_name}'.")

        networks: list[dict[str, Any]] = self.vm_networks_details(vm_name=source_vm_name)

        if not networks:
            raise ValueError(f"Could not find a network for source VM '{source_vm_name}'.")

        network_id: str = networks[0]["net_id"]
        LOGGER.info(f"Using source flavor '{flavor_obj.name}' (ID: {flavor_id}) and network '{network_id}'")

        snapshot: OSP_Image | None = None
        snapshot_name = f"{clone_vm_name}-snapshot"

        created_volume_ids: list[str] = []
        server_created = False

        try:
            LOGGER.info(f"Creating snapshot '{snapshot_name}'...")
            snapshot = self.api.compute.create_server_image(server=source_vm.id, name=snapshot_name, wait=True)

            # Get all volume snapshots created by create_server_image()
            volume_snapshot_name = f"snapshot for {snapshot_name}"
            volume_snapshots = list(self.api.block_storage.snapshots(name=volume_snapshot_name))

            if not volume_snapshots:
                raise ValueError(f"No volume snapshots found for '{volume_snapshot_name}'.")

            LOGGER.info(f"Found {len(volume_snapshots)} volume snapshot(s) for cloning")

            # Sort by device path to preserve attachment order. Fall back to volume ID if device is missing.
            # Device paths like /dev/vda, /dev/vdb naturally sort in attachment order.
            source_volumes_sorted = sorted(
                source_volumes, key=lambda v: next((a.get("device") for a in v.attachments if a.get("device")), v.id)
            )

            if len(source_volumes) != len(volume_snapshots):
                raise ValueError(
                    f"Volume count mismatch for '{source_vm_name}': "
                    f"source VM has {len(source_volumes)} volumes but "
                    f"{len(volume_snapshots)} snapshots were created. "
                    f"Source volumes: {[v.id for v in source_volumes]}, "
                    f"Snapshots: {[s.id for s in volume_snapshots]}"
                )

            snapshot_by_volume_id = {snap.volume_id: snap for snap in volume_snapshots}

            boot_volume = next((vol for vol in source_volumes_sorted if vol.is_bootable), None)

            bdm = []
            next_data_boot_index = 1

            for idx, source_vol in enumerate(source_volumes_sorted):
                vol_snapshot = snapshot_by_volume_id.get(source_vol.id)
                if not vol_snapshot:
                    raise ValueError(
                        f"No snapshot found for source volume '{source_vol.name}' (ID: {source_vol.id}). "
                        f"Available snapshots: {[s.id for s in volume_snapshots]}"
                    )

                is_boot = source_vol.id == boot_volume.id if boot_volume else idx == 0
                volume_size = boot_volume_size if is_boot else source_vol.size
                volume_name_suffix = "boot-vol" if is_boot else f"data-vol-{idx}"
                boot_idx = 0 if is_boot else next_data_boot_index

                LOGGER.info(
                    f"Creating volume '{clone_vm_name}-{volume_name_suffix}' from snapshot "
                    f"(size={volume_size}GB, boot_index={boot_idx})"
                )

                new_volume = self.api.block_storage.create_volume(
                    name=f"{clone_vm_name}-{volume_name_suffix}",
                    snapshot_id=vol_snapshot.id,
                    size=volume_size,
                )
                new_volume = self.api.block_storage.wait_for_status(
                    new_volume, status="available", failures=["error"], wait=300
                )
                created_volume_ids.append(new_volume.id)

                bdm.append({
                    "uuid": new_volume.id,
                    "source_type": "volume",
                    "destination_type": "volume",
                    "boot_index": boot_idx,
                    "delete_on_termination": True,
                })

                if not is_boot:
                    next_data_boot_index += 1

            # Validate exactly one boot volume exists
            if sum(1 for item in bdm if item["boot_index"] == 0) != 1:
                raise ValueError(f"Expected exactly 1 boot volume for '{clone_vm_name}'")

            # Ensure boot volume is first in BDM
            bdm.sort(key=lambda x: x["boot_index"])

            LOGGER.info(f"Creating new server '{clone_vm_name}' from snapshot...")
            new_server: OSP_Server = self.api.compute.create_server(
                name=clone_vm_name,
                image_id="",
                block_device_mapping_v2=bdm,
                flavor_id=flavor_id,
                networks=[{"uuid": network_id}],
            )
            new_server = self.api.compute.wait_for_server(new_server, wait=300)
            server_created = True

            # Track cloned VM for cleanup immediately after creation
            if self.fixture_store:
                self.fixture_store["teardown"].setdefault(self.type, []).append({
                    "name": new_server.name,
                })

            if not power_on:
                LOGGER.info(f"power_on is False, stopping server '{new_server.name}'")
                self.api.compute.stop_server(new_server)
                new_server = self.api.compute.wait_for_server(new_server, status="SHUTOFF")

            LOGGER.info(f"Successfully cloned '{source_vm_name}' to '{clone_vm_name}'")

            return new_server

        finally:
            # Clean up orphaned volumes if server was not created
            if not server_created and created_volume_ids:
                LOGGER.warning(f"Cleaning up {len(created_volume_ids)} orphaned volume(s) after failed clone")
                for vol_id in created_volume_ids:
                    self.api.block_storage.delete_volume(vol_id, ignore_missing=True)

            # Clean up Glance image snapshot if it exists
            if snapshot:
                LOGGER.info(f"Cleaning up Glance image snapshot '{snapshot.name}'...")
                self.api.image.delete_image(snapshot.id, ignore_missing=True)

                # Track volume snapshot for session cleanup - OpenStack prepends "snapshot for " to the name
                volume_snapshot_name = f"snapshot for {snapshot_name}"
                # Find all volume snapshots for cleanup. OpenStack may create multiple volume snapshots
                # with the same name when snapshotting VMs with multiple attached volumes.
                matching_snapshots = list(self.api.block_storage.snapshots(name=volume_snapshot_name))

                # Track all matching snapshots for cleanup
                if matching_snapshots and self.fixture_store:
                    for snap in matching_snapshots:
                        self.fixture_store["teardown"].setdefault("VolumeSnapshot", []).append({
                            "id": snap.id,
                            "name": snap.name,
                        })

    def delete_vm(self, vm_name: str) -> None:
        """
        Finds and deletes a VM instance.

        Args:
            vm_name: The name of the VM to delete.
        """
        LOGGER.info(f"Attempting to delete VM '{vm_name}'")
        vm_to_delete = self.get_instance_obj(name_filter=vm_name)
        if not vm_to_delete:
            LOGGER.warning(f"VM '{vm_name}' not found. Nothing to delete.")
            return

        try:
            self.api.compute.delete_server(vm_to_delete)
            self.api.compute.wait_for_delete(vm_to_delete, interval=2, wait=180)
            LOGGER.info(f"Successfully deleted VM '{vm_name}'.")
        except Exception as e:
            LOGGER.error(f"An error occurred while deleting VM '{vm_name}': {e}")

    def get_vm_by_name(
        self,
        query: str,
        vm_name_suffix: str = "",
        clone_vm: bool = False,
        session_uuid: str = "",
        clone_options: dict | None = None,
    ) -> OSP_Server:
        """
        Retrieves a VM instance by name, optionally cloning if not found.

        Args:
            query: The base name of the VM to retrieve.
            vm_name_suffix: Optional suffix to append to the VM name.
            clone_vm: If True, clone the VM if not found.
            session_uuid: Session UUID for cloning operations.
            clone_options: Additional options for cloning (currently unused).

        Returns:
            OSP_Server: The server object.

        Raises:
            VmNotFoundError: If the VM is not found and clone_vm is False.
        """
        vm_name = f"{query}{vm_name_suffix}"
        LOGGER.info(f"Searching for VM '{vm_name}', {clone_vm=}")
        vm = self.get_instance_obj(name_filter=vm_name)

        if not vm:
            if clone_vm:
                vm = self.clone_vm(
                    source_vm_name=query,
                    clone_vm_name=vm_name,
                    session_uuid=session_uuid,
                )
            else:
                LOGGER.debug(f"VM '{vm_name}' not found in OpenStack.")
                raise VmNotFoundError(f"VM '{vm_name}' not found.")

        return vm

    def stop_vm(self, vm: OSP_Server) -> None:
        """
        Stops a running VM instance.

        Args:
            vm: The server object to stop.
        """
        # Refresh server state to get current status
        current_vm = self.api.compute.get_server(vm.id)

        # Check if VM is already stopped
        if current_vm.status == "SHUTOFF":
            LOGGER.info(f"VM '{vm.name}' is already stopped (SHUTOFF). Skipping stop operation.")
            return

        LOGGER.info(f"Stopping VM '{vm.name}'")
        try:
            self.api.compute.stop_server(vm)
            self.api.compute.wait_for_server(vm, status="SHUTOFF")
            LOGGER.info(f"Successfully stopped VM '{vm.name}'")
        except Exception as e:
            LOGGER.error(f"An error occurred while stopping VM '{vm.name}': {e}")
            raise

    def start_vm(self, vm: OSP_Server) -> None:
        """
        Starts a stopped VM instance.

        Args:
            vm: The server object to start.
        """
        # Refresh server state to get current status
        current_vm = self.api.compute.get_server(vm.id)

        # Check if VM is already running
        if current_vm.status == "ACTIVE":
            LOGGER.info(f"VM '{vm.name}' is already running (ACTIVE). Skipping start operation.")
            return

        LOGGER.info(f"Starting VM '{vm.name}'")
        try:
            self.api.compute.start_server(vm)
            self.api.compute.wait_for_server(vm, status="ACTIVE")
            LOGGER.info(f"Successfully started VM '{vm.name}'")
        except Exception as e:
            LOGGER.error(f"An error occurred while starting VM '{vm.name}': {e}")
            raise

    def get_vm_or_template_networks(
        self,
        names: list[str],
        inventory: ForkliftInventory,
    ) -> list[dict[str, str]]:
        """Delegate to Forklift inventory for OpenStack VMs.

        Args:
            names: List of VM names to query
            inventory: Forklift inventory instance

        Returns:
            List of network mappings
        """
        return inventory.vms_networks_mappings(vms=names)
