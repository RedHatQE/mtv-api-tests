from __future__ import annotations

import copy
from time import sleep
from typing import Any, Self

import humanfriendly
from kubernetes.client.exceptions import ApiException
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.provider import Provider
from ocp_resources.resource import Resource
from ocp_resources.virtual_machine import VirtualMachine
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from libs.base_provider import BaseProvider
from utilities.ssh_utils import VMSSHConnection, create_vm_ssh_connection

LOGGER = get_logger(__name__)


class OCPProvider(BaseProvider):
    def __init__(self, ocp_resource: Provider | None = None, **kwargs: Any) -> None:
        super().__init__(ocp_resource=ocp_resource, **kwargs)
        self.type = Provider.ProviderType.OPENSHIFT

    def connect(self) -> Self:
        return self

    def disconnect(self) -> None:
        LOGGER.info("Disconnecting from OCPProvider source provider")
        pass

    @property
    def test(self) -> bool:
        if not self.ocp_resource:
            raise ValueError("Missing `ocp_resource`")

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

        interfaces = [interface["ipAddress"] for interface in vm.vmi.interfaces if interface["mac"] == mac_address]
        if interfaces:
            return interfaces[0]

        return ""

    @staticmethod
    def start_vm(vm_api: VirtualMachine) -> None:
        if not vm_api.ready:
            try:
                vm_api.start(wait=True)
            except ApiException as exp:
                # 409 means the VM already started
                if exp.status != 409:
                    raise

    @staticmethod
    def stop_vm(vm_api: VirtualMachine) -> None:
        if vm_api.ready:
            vm_api.stop(vmi_delete_timeout=600, wait=True)

    def vm_dict(self, wait_for_guest_agent: bool = False, **kwargs: Any) -> dict[str, Any]:
        if not self.ocp_resource:
            raise ValueError("Missing `ocp_resource`")

        dynamic_client = self.ocp_resource.client
        _source = kwargs.get("source", False)

        cnv_vm_name = f"{kwargs['name']}{kwargs.get('vm_name_suffix', '')}"
        cnv_vm_namespace = kwargs["namespace"]

        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = Resource.ProviderType.OPENSHIFT
        result_vm_info["name"] = cnv_vm_name

        cnv_vm = VirtualMachine(
            client=dynamic_client,
            name=cnv_vm_name,
            namespace=cnv_vm_namespace,
            ensure_exists=True,
        )

        result_vm_info["provider_vm_api"] = cnv_vm
        result_vm_info["id"] = cnv_vm.instance.metadata.uid

        # Wait for VM to reach stable state before recording power state
        # Transitional states (Starting, Stopping, Provisioning, Migrating) would cause incorrect state recording
        STABLE_STATES = (cnv_vm.Status.RUNNING, cnv_vm.Status.STOPPED)

        try:
            for sample in TimeoutSampler(
                wait_timeout=120,
                sleep=2,
                func=lambda: cnv_vm.instance.status.printableStatus in STABLE_STATES,
            ):
                if sample:
                    break
        except TimeoutExpiredError:
            LOGGER.warning(
                f"VM {cnv_vm_name} did not reach stable state within 120s. "
                f"Current state: {cnv_vm.instance.status.printableStatus}"
            )

        # Power state
        result_vm_info["power_state"] = (
            "on" if cnv_vm.instance.status.printableStatus == cnv_vm.Status.RUNNING else "off"
        )

        # Serial number (from firmware) - for serial preservation verification
        firmware_spec: dict[str, Any] | None = cnv_vm.instance.spec.template.spec.domain.get("firmware")
        result_vm_info["serial"] = firmware_spec.get("serial") if firmware_spec else None

        self.start_vm(cnv_vm)
        # True guest agent is reporting all ok
        result_vm_info["guest_agent_running"] = (
            self.wait_for_cnv_vm_guest_agent(vm_dict=result_vm_info) if wait_for_guest_agent else False
        )

        for interface in cnv_vm.vmi.interfaces:
            matching_networks = [
                network for network in cnv_vm.vmi.instance.spec.networks if network.name == interface.name
            ]

            if not matching_networks:
                LOGGER.debug(
                    f"No network found for interface {interface.name} - skipping (likely loopback or system interface)"
                )
                continue

            network = matching_networks[0]
            mac_addr = interface["mac"]
            result_vm_info["network_interfaces"].append({
                "name": interface.name,
                "macAddress": mac_addr,
                "ip": self.get_ip_by_mac_address(mac_address=mac_addr, vm=cnv_vm) if not _source else "",
                "network": "pod" if network.get("pod", False) else network["multus"]["networkName"].split("/")[1],
            })

        disk_idx = 0
        for pvc in cnv_vm.vmi.instance.spec.volumes:
            if not _source:
                name = pvc.persistentVolumeClaim.claimName
            else:
                if pvc.name in ("cloudinitdisk", "cloudInitNoCloud", "cloudinit"):
                    continue

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
                "storage": {
                    "name": _pvc.instance.spec.storageClassName,
                    "access_mode": _pvc.instance.spec.accessModes,
                },
                "device_key": _pvc.name,  # PVC name as unique identifier
                "unit_number": disk_idx,  # Order in volumes list (excluding cloud-init)
            })
            disk_idx += 1

        result_vm_info["cpu"]["num_cores"] = cnv_vm.vmi.instance.spec.domain.cpu.cores
        result_vm_info["cpu"]["num_sockets"] = cnv_vm.vmi.instance.spec.domain.cpu.sockets

        result_vm_info["memory_in_mb"] = int(
            humanfriendly.parse_size(
                cnv_vm.vmi.instance.spec.domain.memory.guest,
                binary=True,
            )
            / 1024
            / 1024
        )

        if result_vm_info["power_state"] == "off":
            self.log.info("Restoring VM Power State (turning off)")
            self.stop_vm(cnv_vm)

        result_vm_info["snapshots_data"] = None

        return result_vm_info

    def clone_vm(self, source_vm_name: str, clone_vm_name: str, session_uuid: str, **kwargs: Any) -> Any:
        return

    def delete_vm(self, vm_name: str) -> Any:
        return

    def create_ssh_connection_to_vm(
        self,
        vm_name: str,
        namespace: str,
        username: str,
        password: str | None = None,
        private_key_path: str | None = None,
        ocp_token: str | None = None,
        ocp_api_server: str | None = None,
        ocp_insecure: bool = False,
        **kwargs: Any,
    ) -> VMSSHConnection:
        """
        Create SSH connection to a VM running on OpenShift.

        Args:
            vm_name: Name of the VM
            namespace: Namespace where VM is running
            username: SSH username
            password: SSH password (if using password auth)
            private_key_path: Path to private key file (if using key auth)
            ocp_token: OCP cluster token for virtctl authentication
            ocp_api_server: OCP API server URL for virtctl authentication
            ocp_insecure: Whether to skip TLS verification for OCP
            **kwargs: Additional arguments passed to VMSSHConnection

        Returns:
            VMSSHConnection: Ready-to-use SSH connection object
        """
        if not self.ocp_resource:
            raise ValueError("Missing `ocp_resource`")

        vm = VirtualMachine(
            client=self.ocp_resource.client,
            name=vm_name,
            namespace=namespace,
            ensure_exists=True,
        )

        return create_vm_ssh_connection(
            vm=vm,
            username=username,
            password=password,
            private_key_path=private_key_path,
            ocp_token=ocp_token,
            ocp_api_server=ocp_api_server,
            ocp_insecure=ocp_insecure,
            **kwargs,
        )
