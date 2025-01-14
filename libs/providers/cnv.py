from __future__ import annotations
import copy

from time import sleep
from typing import Any

from ocp_resources.resource import Resource
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutSampler, TimeoutExpiredError

import humanfriendly
from kubernetes.client import ApiException
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.virtual_machine import VirtualMachine

from libs.base_provider import BaseProvider

LOGGER = get_logger(__name__)


class CNVProvider(BaseProvider):
    def __init__(self, ocp_resource: Resource, **kwargs: Any) -> None:
        super().__init__(ocp_resource=ocp_resource, **kwargs)

    def connect(self) -> "CNVProvider":
        return self

    def disconnect(self) -> None:
        LOGGER.info("Disconnecting CNVProvider source provider")
        pass

    @property
    def test(self) -> bool:
        return bool(self.ocp_resource.exists)

    def wait_for_cnv_vm_guest_agent(self, vm_dict: dict[str, Any], timeout: int = 301) -> bool:
        """
        Wait until the guest agent is Reporting OK Status and return True
        Return False if guest agent is not reporting OK
        """
        status: dict[str, Any] = {}
        conditions: list[dict[str, Any]] = []
        vm_resource = vm_dict.get("provider_vm_api")
        if not vm_resource:
            LOGGER.error(f"VM {vm_dict.get('name')} does not have provider_vm_api")
            return False

        vmi = vm_resource.vmi
        self.log.info(f"Wait until guest agent is active on {vmi.name}")
        sampler = TimeoutSampler(wait_timeout=timeout, sleep=1, func=lambda: vmi.instance)
        try:
            for sample in sampler:
                status = sample.get("status", {})
                conditions = status.get("conditions", {})

                agent_status = [
                    condition
                    for condition in conditions
                    if condition.get("type") == "AgentConnected" and condition.get("status") == "True"
                ]
                if agent_status:
                    return True

        except TimeoutExpiredError:
            self.log.error(
                f"Guest agent is not installed or not active on {vmi.name}. Last status {status}. Last condition: {conditions}"
            )
            return False

        return True

    @staticmethod
    def get_ip_by_mac_address(mac_address: str, vm: VirtualMachine) -> str:
        it_num = 30
        while not vm.vmi.interfaces and it_num > 0:
            sleep(5)
            it_num = it_num - 1

        return [interface["ipAddress"] for interface in vm.vmi.interfaces if interface["mac"] == mac_address][0]

    @staticmethod
    def start_vm(vm_api: VirtualMachine) -> None:
        try:
            if not vm_api.ready:
                vm_api.start(wait=True)
        except ApiException as e:
            # if vm is already running, do nothing.
            if e.status != 409:
                raise

    @staticmethod
    def stop_vm(vm_api: VirtualMachine) -> None:
        if vm_api.ready:
            vm_api.stop(vmi_delete_timeout=600, wait=True)

    def vm_dict(self, wait_for_guest_agent: bool = False, **kwargs: Any) -> dict[str, Any]:
        dynamic_client = self.ocp_resource.client
        source = kwargs.get("source", False)

        cnv_vm_name = kwargs["name"]
        cnv_vm_namespace = kwargs["namespace"]

        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = Resource.ProviderType.OPENSHIFT
        result_vm_info["name"] = cnv_vm_name

        cnv_vm = VirtualMachine(
            client=dynamic_client,
            name=cnv_vm_name,
            namespace=cnv_vm_namespace,
        )

        if not cnv_vm.exists:
            raise ValueError(f"VM {cnv_vm_name} does not exist in namespace {cnv_vm_namespace}")

        result_vm_info["provider_vm_api"] = cnv_vm

        # Power state
        result_vm_info["power_state"] = "on" if cnv_vm.instance.spec.running else "off"

        if not source:
            # This step is required to check some of the vm_signals.
            self.start_vm(cnv_vm)

            # True guest agent is reporting all ok
            result_vm_info["guest_agent_running"] = (
                self.wait_for_cnv_vm_guest_agent(vm_dict=result_vm_info) if wait_for_guest_agent else False
            )

        for interface in cnv_vm.get_interfaces():
            network = [
                network for network in cnv_vm.instance.spec.template.spec.networks if network.name == interface.name
            ][0]
            result_vm_info["network_interfaces"].append({
                "name": interface.name,
                "macAddress": interface.macAddress,
                "ip": self.get_ip_by_mac_address(mac_address=interface.macAddress, vm=cnv_vm) if not source else "",
                "network": "pod" if network.get("pod", False) else network["multus"]["networkName"].split("/")[1],
            })

        for pvc in cnv_vm.instance.spec.template.spec.volumes:
            if not source:
                name = pvc.persistentVolumeClaim.claimName
            else:
                if pvc.name == "cloudinitdisk":
                    continue
                else:
                    name = pvc.dataVolume.name

            _pvc = PersistentVolumeClaim(
                namespace=cnv_vm.namespace,
                name=name,
                client=dynamic_client,
            )
            result_vm_info["disks"].append({
                "name": _pvc.name,
                "size_in_kb": int(
                    humanfriendly.parse_size(_pvc.instance.spec.resources.requests.storage, binary=True) / 1024
                ),
                "storage": {"name": _pvc.instance.spec.storageClassName, "access_mode": _pvc.instance.spec.accessModes},
            })

        result_vm_info["cpu"]["num_cores"] = cnv_vm.instance.spec.template.spec.domain.cpu.cores
        result_vm_info["cpu"]["num_sockets"] = cnv_vm.instance.spec.template.spec.domain.cpu.sockets

        result_vm_info["memory_in_mb"] = int(
            humanfriendly.parse_size(
                cnv_vm.instance.spec.template.spec.domain.memory.guest,
                binary=True,
            )
            / 1024
            / 1024
        )
        if not source and result_vm_info["power_state"] == "off":
            self.log.info("Restoring VM Power State (turning off)")
            self.stop_vm(cnv_vm)

        result_vm_info["snapshots_data"] = None

        return result_vm_info
