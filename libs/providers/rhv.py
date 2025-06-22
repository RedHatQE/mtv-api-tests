from __future__ import annotations

import copy
import os
from typing import Any

import ovirtsdk4
from ocp_resources.provider import Provider
from ocp_resources.resource import Resource
from ovirtsdk4 import NotFoundError
from ovirtsdk4.types import VmStatus
from simple_logger.logger import get_logger

from exceptions.exceptions import OvirtMTVDatacenterNotFoundError, OvirtMTVDatacenterStatusError
from libs.base_provider import BaseProvider

LOGGER = get_logger(__name__)


class OvirtProvider(BaseProvider):
    """
    https://github.com/oVirt/ovirt-engine-sdk/tree/master/sdk/examples
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        ocp_resource: Provider,
        ca_file: str | None = None,
        insecure: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            ocp_resource=ocp_resource,
            host=host,
            username=username,
            password=password,
            **kwargs,
        )
        self.type = Provider.ProviderType.RHV
        self.insecure = insecure
        self.ca_file = ca_file
        if self.ca_file and not self.insecure:
            if not os.path.isfile(self.ca_file):
                raise ValueError("ca_file must be a valid path to a file")

        self.vm_cash: dict[str, Any] = {}
        self.VM_POWER_OFF_CODE: int = 33

    def disconnect(self) -> None:
        LOGGER.info(f"Disconnecting OvirtProvider source provider {self.host}")
        self.api.close()

    def connect(self) -> "OvirtProvider":
        self.api = ovirtsdk4.Connection(
            url=self.host,
            username=self.username,
            password=self.password,
            ca_file=self.ca_file if not self.insecure else None,
            debug=self.debug,
            log=self.log,
            insecure=self.insecure,
        )
        if not self.is_mtv_datacenter_ok:
            raise OvirtMTVDatacenterStatusError()
        return self

    @property
    def test(self) -> bool:
        return self.api.test()

    @property
    def vms_services(self) -> ovirtsdk4.services.VmsService:
        return self.api.system_service().vms_service()

    @property
    def disks_service(self) -> ovirtsdk4.services.DisksService:
        return self.api.system_service().disks_service()

    @property
    def network_services(self) -> ovirtsdk4.services.NetworksService:
        return self.api.system_service().networks_service()

    @property
    def storage_services(self) -> ovirtsdk4.services.StorageDomainsService:
        return self.api.system_service().storage_domains_service()

    @property
    def data_center_service(self) -> ovirtsdk4.services.DataCentersService:
        return self.api.system_service().data_centers_service()

    def events_service(self) -> ovirtsdk4.services.EventsService:
        return self.api.system_service().events_service()

    def events_list_by_vm(self, vm: Any) -> Any:
        return self.events_service().list(search=f"Vms.id = {vm.id}")

    def vms(self, search: str) -> Any:
        return self.vms_services.list(search=search)

    def vm(self, name: str, cluster: str | None = None) -> Any:
        query = f"name={name}"
        if cluster:
            query = f"{query} cluster={cluster}"

        return self.vms(search=query)[0]

    def vm_nics(self, vm: Any) -> list[Any]:
        return [self.api.follow_link(nic) for nic in self.vms_services.vm_service(id=vm.id).nics_service().list()]

    def vm_disk_attachments(self, vm: Any) -> list[Any]:
        return [
            self.api.follow_link(disk.disk)
            for disk in self.vms_services.vm_service(id=vm.id).disk_attachments_service().list()
        ]

    def list_snapshots(self, vm: Any) -> list[Any]:
        snapshots = []
        for snapshot in self.vms_services.vm_service(id=vm.id).snapshots_service().list():
            try:
                _snapshot = self.api.follow_link(snapshot)
                snapshots.append(_snapshot)
            except NotFoundError:
                continue
        return snapshots

    def start_vm(self, vm: Any) -> None:
        if vm.status != VmStatus.UP:
            self.vms_services.vm_service(vm.id).start()

    # TODO: change the function definition to shutdown_vm once we will have the same for VMware
    def stop_vm(self, vm: Any) -> None:
        if vm.status == VmStatus.UP:
            self.vms_services.vm_service(vm.id).shutdown()

    @property
    def networks_name(self) -> list[str]:
        return [f"{network.name}/{network.name}" for network in self.network_services.list()]

    @property
    def networks_id(self) -> list[str]:
        return [network.id for network in self.network_services.list()]

    @property
    def networks(self) -> list[dict[str, Any]]:
        return [
            {"name": network.name, "id": network.id, "data_center": self.api.follow_link(network.data_center).name}
            for network in self.network_services.list()
        ]

    @property
    def storages_name(self) -> list[str]:
        return [storage.name for storage in self.storage_services.list()]

    @property
    def storage_groups(self) -> list[dict[str, Any]]:
        return [{"name": storage.name, "id": storage.id} for storage in self.storage_services.list()]

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        source_vm = self.vms(search=kwargs["name"])[0]

        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = Resource.ProviderType.RHV
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = kwargs["name"]

        # Network Interfaces
        for nic in self.vm_nics(vm=source_vm):
            network = self.api.follow_link(self.api.follow_link(nic.__getattribute__("vnic_profile")).network)
            result_vm_info["network_interfaces"].append({
                "name": nic.name,
                "macAddress": nic.mac.address,
                "network": {"name": network.name, "id": network.id},
            })

        # Disks
        for disk in self.vm_disk_attachments(vm=source_vm):
            storage_domain = self.api.follow_link(disk.storage_domains[0])
            result_vm_info["disks"].append({
                "name": disk.name,
                "size_in_kb": disk.total_size,
                "storage": dict(name=storage_domain.name, id=storage_domain.id),
            })
        # CPUs
        result_vm_info["cpu"]["num_cores"] = source_vm.cpu.topology.cores
        result_vm_info["cpu"]["num_threads"] = source_vm.cpu.topology.threads
        result_vm_info["cpu"]["num_sockets"] = source_vm.cpu.topology.sockets

        # Memory
        result_vm_info["memory_in_mb"] = source_vm.memory / 1024 / 1024

        # Snapshots details
        for snapshot in self.list_snapshots(source_vm):
            result_vm_info["snapshots_data"].append(
                dict({
                    "description": snapshot.description,
                    "id": snapshot.id,
                    "snapshot_status": snapshot.snapshot_status,
                    "snapshot_type": snapshot.snapshot_type,
                })
            )

        # Power state
        if source_vm.status == VmStatus.UP:
            result_vm_info["power_state"] = "on"
        elif source_vm.status == VmStatus.DOWN:
            result_vm_info["power_state"] = "off"
        else:
            result_vm_info["power_state"] = "other"
        return result_vm_info

    def check_for_power_off_event(self, vm: Any) -> bool:
        events = self.events_list_by_vm(vm)
        for event in events:
            if event.code == self.VM_POWER_OFF_CODE:
                return True
        return False

    @property
    def mtv_datacenter(self) -> ovirtsdk4.types.DataCenter:
        for dc in self.data_center_service.list():
            if dc.name == "MTV-CNV":
                return dc

        raise OvirtMTVDatacenterNotFoundError()

    @property
    def is_mtv_datacenter_ok(self) -> bool:
        return self.mtv_datacenter.status.value == "up"
