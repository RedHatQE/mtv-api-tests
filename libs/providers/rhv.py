from __future__ import annotations

import copy
import os
from typing import Any, Callable, Self

import ovirtsdk4
from ocp_resources.provider import Provider
from ocp_resources.resource import Resource
from ovirtsdk4 import NotFoundError, types
from ovirtsdk4.types import VmStatus
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

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
        ocp_resource: Provider | None = None,
        ca_file: str | None = None,
        insecure: bool = True,
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

    def connect(self) -> Self:
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

    @property
    def templates_service(self) -> ovirtsdk4.services.TemplatesService:
        return self.api.system_service().templates_service()

    def events_service(self) -> ovirtsdk4.services.EventsService:
        return self.api.system_service().events_service()

    def events_list_by_vm(self, vm: types.Vm) -> Any:
        return self.events_service().list(search=f"Vms.id = {vm.id}")

    def get_vm_by_name(self, name: str, cluster: str | None = None) -> Any:
        query = f"name={name}"
        if cluster:
            query = f"{query} cluster={cluster}"

        return self.vms_services.list(search=query)[0]

    def vm_nics(self, vm: types.Vm) -> list[Any]:
        return [self.api.follow_link(nic) for nic in self.vms_services.vm_service(id=vm.id).nics_service().list()]

    def vm_disk_attachments(self, vm: types.Vm) -> list[Any]:
        return [
            self.api.follow_link(disk.disk)
            for disk in self.vms_services.vm_service(id=vm.id).disk_attachments_service().list()
        ]

    def list_snapshots(self, vm: types.Vm) -> list[Any]:
        snapshots = []
        for snapshot in self.vms_services.vm_service(id=vm.id).snapshots_service().list():
            try:
                _snapshot = self.api.follow_link(snapshot)
                snapshots.append(_snapshot)
            except NotFoundError:
                continue
        return snapshots

    def start_vm(self, vm: types.Vm) -> None:
        vm_service = self.vms_services.vm_service(vm.id)
        if vm_service.get().status != VmStatus.UP:
            LOGGER.info(f"Starting VM '{vm.name}'")
            vm_service.start()
            self._wait_for_condition(
                entity_name=vm.name,
                action_name="Power On",
                # CORRECTED: Use the vm_service to get the latest status
                condition_func=lambda: vm_service.get().status == types.VmStatus.UP,
            )

    def stop_vm(self, vm: types.Vm) -> None:
        vm_service = self.vms_services.vm_service(vm.id)
        if vm_service.get().status == VmStatus.UP:
            LOGGER.info(f"Stopping VM '{vm.name}'")
            vm_service.shutdown()
            self._wait_for_condition(
                entity_name=vm.name,
                action_name="Power Off",
                # CORRECTED: Use the vm_service to get the latest status
                condition_func=lambda: vm_service.get().status == types.VmStatus.DOWN,
            )

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        target_vm_name = f"{kwargs['name']}{kwargs.get('vm_name_suffix', '')}"
        try:
            source_vm = self.get_vm_by_name(name=target_vm_name)
        except IndexError:
            # VM not found - clone it if clone flag is set
            if kwargs.get("clone"):
                source_vm = self.clone_vm(
                    source_vm_name=kwargs["name"], clone_vm_name=target_vm_name, session_uuid=kwargs["session_uuid"]
                )
            else:
                raise

        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = Resource.ProviderType.RHV
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = source_vm.name
        result_vm_info["id"] = source_vm.id

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
                "device_key": disk.id,  # RHV disk ID
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

    def check_for_power_off_event(self, vm: types.Vm) -> bool:
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

    def _wait_for_condition(
        self,
        entity_name: str,
        action_name: str,
        condition_func: Callable[[], bool],
        timeout: int = 60 * 10,
        sleep: int = 1,
    ) -> None:
        """
        Waits for a specific condition to be True using TimeoutSampler.

        Args:
            entity_name: The name of the entity being waited on.
            action_name: The name of the action being performed.
            condition_func: A function that returns True when the condition is met.
            timeout: The total time to wait in seconds.
            sleep: The interval between checks in seconds.
        """
        LOGGER.info(f"Waiting for '{action_name}' on '{entity_name}' to complete...")
        try:
            for sample in TimeoutSampler(
                wait_timeout=timeout,
                sleep=sleep,
                func=condition_func,
            ):
                if sample:
                    LOGGER.info(f"Action '{action_name}' on '{entity_name}' completed successfully.")
                    return
        except TimeoutExpiredError:
            LOGGER.error(f"Timeout expired while waiting for '{action_name}' on '{entity_name}'.")
            raise

    def delete_vm(self, vm_name: str) -> None:
        """
        Finds a VM by name, powers it off if necessary, and deletes it.
        """
        LOGGER.info(f"Attempting to delete VM '{vm_name}'")
        try:
            vm = self.get_vm_by_name(name=vm_name)
        except IndexError:
            LOGGER.warning(f"VM '{vm_name}' not found. Nothing to delete.")
            return

        vm_service = self.vms_services.vm_service(vm.id)
        if vm.status != types.VmStatus.DOWN:
            self.stop_vm(vm)

        LOGGER.info(f"Deleting VM '{vm_name}' and its disks...")
        vm_service.remove()

        def _check_vm_deleted():
            try:
                vm_service.get()
                return False
            except NotFoundError:
                return True

        self._wait_for_condition(
            entity_name=vm_name,
            action_name="Deletion",
            condition_func=_check_vm_deleted,
        )

    def get_template_by_name(self, name: str) -> Any:
        """Get template by name from oVirt."""
        query = f"name={name}"
        templates = self.templates_service.list(search=query)
        if not templates:
            raise NotFoundError(f"Template '{name}' not found in oVirt")
        return templates[0]

    def clone_vm(
        self,
        source_vm_name: str,
        clone_vm_name: str,
        session_uuid: str,
        power_on: bool = False,
        **kwargs: Any,
    ) -> types.Vm:
        """
        Clones a VM from a template.
        In RHV/oVirt, source_vm_name is actually a template name.
        Raises an exception if the process fails.
        """
        clone_vm_name = f"{session_uuid}-{clone_vm_name}"
        LOGGER.info(f"Starting clone of '{source_vm_name}' template to '{clone_vm_name}'")

        # Get the template (not VM)
        try:
            template = self.get_template_by_name(name=source_vm_name)
            LOGGER.info(f"Using template '{source_vm_name}' (ID: {template.id}) for cloning")
        except NotFoundError:
            LOGGER.error(f"Template '{source_vm_name}' not found in oVirt")
            raise

        try:
            # Clone from template
            new_vm = self.vms_services.add(
                vm=types.Vm(
                    name=clone_vm_name,
                    cluster=types.Cluster(id=template.cluster.id),
                    template=types.Template(id=template.id),
                    memory_policy=types.MemoryPolicy(guaranteed=0),
                ),
                clone=True,
            )
            new_vm_service = self.vms_services.vm_service(new_vm.id)

            # Wait for VM to be ready
            self._wait_for_condition(
                entity_name=clone_vm_name,
                action_name="VM Creation from Template",
                condition_func=lambda: new_vm_service.get().status == types.VmStatus.DOWN,
                timeout=60 * 20,
            )

            # Track cloned VM for cleanup
            if self.fixture_store:
                self.fixture_store["teardown"].setdefault(self.type, []).append({
                    "name": clone_vm_name,
                })

            if power_on:
                self.start_vm(new_vm)

            LOGGER.info(f"Successfully cloned template '{source_vm_name}' to VM '{clone_vm_name}'")
            return new_vm

        except Exception as e:
            LOGGER.error(f"Clone process failed: {e}")
            raise
