import abc
from contextlib import contextmanager
import uuid
from datetime import datetime
import copy
from pathlib import Path
from subprocess import check_output, STDOUT
from time import sleep
from typing import Any, Generator, Optional, Tuple

from ocp_resources.exceptions import MissingResourceResError
from ocp_resources.provider import Provider
from ocp_resources.resource import DynamicClient
import pytest
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutSampler, TimeoutExpiredError

import libs.providers as providers
import humanfriendly
from kubernetes.client import ApiException
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.virtual_machine import VirtualMachine
from ocp_resources.mtv import MTV
from ocp_resources.secret import Secret
from ovirtsdk4.types import VmStatus
from pyVmomi import vim
import threading

LOGGER = get_logger(__name__)


def get_guest_os_credentials(provider_data, vm_dict):
    user = provider_data["guest_vm_linux_user"]
    password = provider_data["guest_vm_linux_password"]

    if vm_dict["win_os"]:
        user = provider_data["guest_vm_win_user"]
        password = provider_data["guest_vm_win_password"]
    return user, password


class MTVProvider(abc.ABC):
    """
    MTV Provider base helper class

    Args:
        cr (:ocp_resource.Provider:): MTV Provider namespace resource
        provider_api (:providers.Provider:): Provider
        API (RHV, VMWare, Openshift) provider_data (dict): Providers Details, Such as host, server credentials,
        guest_os credentials, etc. # TODO: Move the responsibility of creating the CR, Secrets, Provider_API connection
        from the fixtures to this MTV Provider classes.

    """

    # Unified Representation of a VM of All Provider Types
    VIRTUAL_MACHINE_TEMPLATE: dict[str, Any] = {
        "id": "",
        "name": "",
        "provider_type": "",  # "ovirt" / "vsphere" / "openstack"
        "provider_vm_api": None,
        "network_interfaces": [],
        "disks": [],
        "cpu": {},
        "memory_in_mb": 0,
        "snapshots_data": [],
        "power_state": "",
    }

    # In order to avoid costly request to the provider, we cash the dict object vm representation return by vm_dict
    # Key is the vm_name
    def __init__(self, cr, provider_api, provider_data=None):
        self.cr = cr
        self.provider_api = provider_api
        self.provider_data = provider_data

    @abc.abstractmethod
    def vm_dict(self, **xargs):
        """
        Create a dict for a single vm holding the Network Interface details, Disks and Storage, etc..
        """


class MTVVMwareProvider(MTVProvider):
    def __init__(self, cr, provider_api, provider_data=None):
        super().__init__(cr=cr, provider_api=provider_api, provider_data=provider_data)
        self.vm_cash = {}

    def vm_dict(self, **xargs):
        vm_name = xargs["name"]
        source_vm = self.provider_api.vms(search=f"^{vm_name}$", folder=self.provider_data.get("vm_folder"))[0]
        result_vm_info = copy.deepcopy(MTVProvider.VIRTUAL_MACHINE_TEMPLATE)
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
        for snapshot in self.provider_api.list_snapshots(source_vm):
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

            current_file_content = self.provider_api.download_file_from_guest_vm(
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

                self.provider_api.upload_file_to_guest_vm(
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

            self.provider_api.upload_file_to_guest_vm(
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


class MTVOvirtProvider(MTVProvider):
    def __init__(self, cr, provider_api, provider_data=None):
        super().__init__(cr=cr, provider_api=provider_api, provider_data=provider_data)
        self.vm_cash = {}
        self.VM_POWER_OFF_CODE = 33

    def vm_dict(self, **xargs):
        source_vm = self.provider_api.vms(search=xargs["name"])[0]

        result_vm_info = copy.deepcopy(MTVProvider.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = MTV.ProviderType.RHV
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = xargs["name"]

        # Network Interfaces
        for nic in self.provider_api.vm_nics(vm=source_vm):
            network = self.provider_api.api.follow_link(
                self.provider_api.api.follow_link(nic.__getattribute__("vnic_profile")).network
            )
            result_vm_info["network_interfaces"].append({
                "name": nic.name,
                "macAddress": nic.mac.address,
                "network": {"name": network.name, "id": network.id},
            })

        # Disks
        for disk in self.provider_api.vm_disk_attachments(vm=source_vm):
            storage_domain = self.provider_api.api.follow_link(disk.storage_domains[0])
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
        for snapshot in self.provider_api.list_snapshots(source_vm):
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

    def check_for_power_off_event(self, vm):
        events = self.provider_api.events_list_by_vm(vm)
        for event in events:
            if event.code == self.VM_POWER_OFF_CODE:
                return True
        return False


class MTVOpenStackProvider(MTVProvider):
    def __init__(self, cr, provider_api, provider_data=None):
        super().__init__(cr=cr, provider_api=provider_api, provider_data=provider_data)
        self.vm_cash = {}

    def vm_dict(self, **xargs):
        vm_name = xargs["name"]
        source_vm = self.provider_api.get_instance_obj(vm_name)
        result_vm_info = copy.deepcopy(MTVProvider.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = "openstack"
        result_vm_info["provider_vm_api"] = source_vm
        result_vm_info["name"] = xargs["name"]

        # Snapshots details
        for volume_snapshots in self.provider_api.list_snapshots(vm_name):
            for snapshot in volume_snapshots:
                result_vm_info["snapshots_data"].append({
                    "description": snapshot.name,
                    "id": snapshot.id,
                    "snapshot_status": snapshot.status,
                })

        # Network Interfaces
        vm_networks_details = self.provider_api.vm_networks_details(vm_name=vm_name)
        for network, details in zip(self.provider_api.list_network_interfaces(vm_name=vm_name), vm_networks_details):
            if network.network_id == details["net_id"]:
                result_vm_info["network_interfaces"].append({
                    "name": details["net_name"],
                    "macAddress": network.mac_address,
                    "network": {"name": details["net_name"], "id": network.network_id},
                })

        # Disks
        for disk in self.provider_api.list_volumes(vm_name=vm_name):
            result_vm_info["disks"].append({
                "name": disk.name,
                "size_in_kb": disk.size,
                "storage": dict(name=disk.availability_zone, id=disk.id),
            })

        # CPUs
        volume_metadata = self.provider_api.get_volume_metadata(vm_name=vm_name)
        result_vm_info["cpu"]["num_cores"] = int(volume_metadata["hw_cpu_cores"])
        result_vm_info["cpu"]["num_threads"] = int(volume_metadata["hw_cpu_threads"])
        result_vm_info["cpu"]["num_sockets"] = int(volume_metadata["hw_cpu_sockets"])

        # Memory
        flavor = self.provider_api.get_flavor_obj(vm_name=vm_name)
        result_vm_info["memory_in_mb"] = flavor.ram

        # Power state
        if source_vm.status == "ACTIVE":
            result_vm_info["power_state"] = "on"
        elif source_vm.status == "SHUTOFF":
            result_vm_info["power_state"] = "off"
        else:
            result_vm_info["power_state"] = "other"
        return result_vm_info


class MTVCNVProvider(MTVProvider):
    @staticmethod
    def wait_for_cnv_vm_guest_agent(vm_dict, timeout=300):
        """
        Wait until the guest agent is Reporting OK Status and return True
        Return False if guest agent is not reporting OK
        """
        status: dict[str, Any] = {}
        conditions: list[dict[str, Any]] = []
        vmi = vm_dict.get("provider_vm_api").vmi
        LOGGER.info(f"Wait until guest agent is active on {vmi.name}")
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
            LOGGER.error(
                f"Guest agent is not installed or not active on {vmi.name}. Last status {status}. Last condition: {conditions}"
            )
            return False

    @staticmethod
    def get_ip_by_mac_address(mac_address, vm):
        it_num = 30
        while not vm.vmi.interfaces and it_num > 0:
            sleep(5)
            it_num = it_num - 1
        return [interface["ipAddress"] for interface in vm.vmi.interfaces if interface["mac"] == mac_address][0]

    @staticmethod
    def start_vm(vm_api):
        try:
            if not vm_api.ready:
                vm_api.start(wait=True)
        except ApiException as e:
            # if vm is already running, do nothing.
            if e.status != 409:
                raise

    @staticmethod
    def stop_vm(vm_api):
        if vm_api.ready:
            vm_api.stop(vmi_delete_timeout=600, wait=True)

    def vm_dict(self, wait_for_guest_agent=False, **xargs):
        dynamic_client = self.provider_api
        source = xargs.get("source", False)

        result_vm_info = copy.deepcopy(MTVProvider.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = MTV.ProviderType.OPENSHIFT
        result_vm_info["name"] = xargs["name"]

        # keeping a ref to the SDK vm object
        cnv_vm = next(
            VirtualMachine.get(
                dyn_client=dynamic_client,
                name=xargs["name"],
                namespace=xargs["namespace"],
            )
        )
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
            _pvc = next(
                PersistentVolumeClaim.get(
                    namespace=cnv_vm.namespace,
                    name=name,
                    dyn_client=dynamic_client,
                )
            )
            result_vm_info["disks"].append({
                "name": _pvc.name,
                "size_in_kb": int(
                    humanfriendly.parse_size(_pvc.instance.spec.resources.requests.storage, binary=True) / 1024
                ),
                "storage": dict(name=_pvc.instance.spec.storageClassName, access_mode=_pvc.instance.spec.accessModes),
                # "vddk_url": _dv.instance.spec.source.vddk.url if _dv.instance.spec.source.vddk else None
            })

        result_vm_info["cpu"]["num_cores"] = cnv_vm.instance.spec.template.spec.domain.cpu.cores
        result_vm_info["cpu"]["num_sockets"] = cnv_vm.instance.spec.template.spec.domain.cpu.sockets

        result_vm_info["memory_in_mb"] = int(
            humanfriendly.parse_size(
                cnv_vm.instance.spec.template.spec.domain.resources.requests.memory,
                binary=True,
            )
            / 1024
            / 1024
        )
        if not source and result_vm_info["power_state"] == "off":
            LOGGER.info("Restoring VM Power State (turning off)")
            self.stop_vm(cnv_vm)

        result_vm_info["snapshots_data"] = None

        return result_vm_info


def create_ocp_resource_if_not_exists(dyn_client, resource, **xargs):
    """
    Create The Openshift Resource If it does not exists

      Return:
          The k8 resource

    """
    try:
        xargs.update({"client": dyn_client})
        resource(**xargs).create()
    except ApiException as e:
        # if resource already found in cluster, just ignore.
        if e.status != 409:
            raise
    return next(resource.get(dyn_client=dyn_client, **xargs))


def vmware_provider(provider_data):
    return provider_data["type"] == Provider.ProviderType.VSPHERE


def rhv_provider(provider_data):
    return provider_data["type"] == Provider.ProviderType.RHV


def openstack_provider(provider_data):
    return provider_data["type"] == "openstack"


def ova_provider(provider_data):
    return provider_data["type"] == "ova"


def generate_ca_cert_file(provider_data: dict[str, Any], cert_file: Path) -> str:
    cert = check_output(
        [
            "/bin/sh",
            "-c",
            f"openssl s_client -connect {provider_data['fqdn']}:443 -showcerts < /dev/null",
        ],
        stderr=STDOUT,
    )

    cert_file.write_bytes(cert)
    return str(cert_file)


def fetch_thumbprint(provider_data):
    if vmware_provider(provider_data):
        t = check_output(
            [
                "/bin/sh",
                "-c",
                f"openssl s_client -connect {provider_data['fqdn']}:443 </dev/null 2>/dev/null | openssl x509 "
                f"-fingerprint -noout -in /dev/stdin | cut -d '=' -f 2 | tr -d $'\n'",
            ],
            stderr=STDOUT,
        )
        return str(t).split("'")[1]


def is_true(value):
    if isinstance(value, str):
        return value.lower() in ["true", "1", "t", "y", "yes"]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return False


def background(f):
    """
    a threading decorator
    use @background above the function you want to run in the background
    """

    def backgrnd_func(*a, **kw):
        threading.Thread(target=f, args=a, kwargs=kw).start()

    return backgrnd_func


class CustomizedSecret(Secret):
    """
    Customized secret object.
    """

    def __init__(
        self,
        name=None,
        namespace=None,
        client=None,
        accesskeyid=None,
        secretkey=None,
        htpasswd=None,
        teardown=True,
        data_dict=None,
        string_data=None,
        yaml_file=None,
        type=None,
        metadata=None,
        **kwargs,
    ):
        super().__init__(
            name=name,
            namespace=namespace,
            client=client,
            accesskeyid=accesskeyid,
            secretkey=secretkey,
            htpasswd=htpasswd,
            teardown=teardown,
            data_dict=data_dict,
            string_data=string_data,
            yaml_file=yaml_file,
            type=type,
            **kwargs,
        )
        self.metadata = metadata

    def to_dict(self):
        super().to_dict()
        if not self.yaml_file:
            if self.metadata:
                self.res["metadata"].update(self.metadata)


class MTVOvaProvider(MTVProvider):
    def __init__(self, cr, provider_api, provider_data=None):
        super().__init__(cr=cr, provider_api=provider_api, provider_data=provider_data)
        self.vm_cash = {}

    def vm_dict(self, **xargs):
        return True


def gen_network_map_list(
    config: dict[str, Any],
    source_provider_data: dict[str, Any],
    multus_network_name: str = "",
    pod_only: bool = False,
) -> list[dict[str, dict[str, str]]]:
    network_map_list: list[dict[str, dict[str, str]]] = []
    _destination_pod: dict[str, str] = {"type": "pod"}
    _destination_multus: dict[str, str] = {
        "name": multus_network_name,
        "namespace": config["target_namespace"],
        "type": "multus",
    }
    _destination: dict[str, str] = _destination_pod

    for index, network in enumerate(source_provider_data["networks"]):
        if not pod_only:
            if index > 0:
                _destination = _destination_multus
            else:
                _destination = _destination_pod

        network_map_list.append({
            "destination": _destination,
            "source": network,
        })
    return network_map_list


def provider_cr_name(provider_data, username):
    name = (
        f"{provider_data['type']}-{provider_data['version'].replace('.', '-')}-"
        f"{provider_data['fqdn'].split('.')[0]}-{username.split('@')[0]}"
    )
    return generate_time_based_uuid_name(name=name)


@contextmanager
def create_source_provider(
    config: dict[str, Any],
    source_provider_data: dict[str, Any],
    mtv_namespace: str,
    admin_client: DynamicClient,
    tmp_dir: Optional[pytest.TempPathFactory] = None,
    **kwargs: dict[str, Any],
) -> Generator[Tuple[MTVProvider, Any, Any], None, None]:
    # common
    source_provider_data_copy = copy.deepcopy(source_provider_data)
    if config["source_provider_type"] == Provider.ProviderType.OPENSHIFT:
        provider = Provider(name="host", namespace=mtv_namespace, client=admin_client)
        if not provider.exists:
            raise MissingResourceResError(f"Provider {provider.name} not found")

        yield (
            MTVCNVProvider(
                cr=provider,
                provider_api=admin_client,
                provider_data=source_provider_data_copy,
            ),
            None,
            None,
        )

    else:
        for key, value in kwargs.items():
            source_provider_data_copy[key] = value

        name = provider_cr_name(provider_data=source_provider_data_copy, username=source_provider_data_copy["username"])
        secret_string_data = {}
        provider_args = {
            "username": source_provider_data_copy["username"],
            "password": source_provider_data_copy["password"],
        }
        provider_client: Any = None
        mtv_provider_object: Any = None
        metadata_labels = {
            "labels": {
                "createdForProviderType": source_provider_data_copy["type"],
            }
        }
        # vsphere/vmware
        if vmware_provider(provider_data=source_provider_data_copy):
            provider_args["host"] = source_provider_data_copy["fqdn"]
            provider_client = providers.VMWare
            mtv_provider_object = MTVVMwareProvider
            secret_string_data["user"] = source_provider_data_copy["username"]
            secret_string_data["password"] = source_provider_data_copy["password"]
        # rhv/ovirt
        elif rhv_provider(provider_data=source_provider_data_copy):
            if not tmp_dir:
                raise ValueError("tmp_dir is required for rhv")

            cert_file = generate_ca_cert_file(
                provider_data=source_provider_data_copy,
                cert_file=tmp_dir.mktemp(source_provider_data_copy["type"].upper())
                / f"{source_provider_data_copy['type']}_cert.crt",
            )
            provider_args["host"] = source_provider_data_copy["api_url"]
            provider_args["ca_file"] = cert_file
            provider_client = providers.RHV
            mtv_provider_object = MTVOvirtProvider
            secret_string_data["user"] = source_provider_data_copy["username"]
            secret_string_data["password"] = source_provider_data_copy["password"]
            secret_string_data["cacert"] = Path(cert_file).read_text()
        # openstack
        elif openstack_provider(provider_data=source_provider_data_copy):
            provider_args["host"] = source_provider_data_copy["api_url"]
            provider_args["auth_url"] = source_provider_data_copy["api_url"]
            provider_args["project_name"] = source_provider_data_copy["project_name"]
            provider_args["user_domain_name"] = source_provider_data_copy["user_domain_name"]
            provider_args["region_name"] = source_provider_data_copy["region_name"]
            provider_args["user_domain_id"] = source_provider_data_copy["user_domain_id"]
            provider_args["project_domain_id"] = source_provider_data_copy["project_domain_id"]
            provider_client = providers.OpenStack
            mtv_provider_object = MTVOpenStackProvider
            secret_string_data["username"] = source_provider_data_copy["username"]
            secret_string_data["password"] = source_provider_data_copy["password"]
            secret_string_data["regionName"] = source_provider_data_copy["region_name"]
            secret_string_data["projectName"] = source_provider_data_copy["project_name"]
            secret_string_data["domainName"] = source_provider_data_copy["user_domain_name"]
        elif ova_provider(provider_data=source_provider_data_copy):
            provider_args["host"] = source_provider_data_copy["api_url"]
            provider_client = providers.OVA
            mtv_provider_object = MTVOvaProvider

        secret_string_data["url"] = source_provider_data_copy["api_url"]
        secret_string_data["insecureSkipVerify"] = config["insecure_verify_skip"]

        if not provider_client:
            raise ValueError("Failed to get provider clent")

        if not mtv_provider_object:
            raise ValueError("Failed to get provider object")

        # this is for communication with the provider
        with provider_client(**provider_args) as _provider_client:
            if not _provider_client.test:
                pytest.skip(f"Skipping VM import tests: {provider_args['host']} is not available.")

            # Creating the source Secret and source Provider CRs
            customized_secret = CustomizedSecret(
                client=admin_client,
                name=name,
                namespace=mtv_namespace,
                string_data=secret_string_data,
                metadata=metadata_labels,
            )
            customized_secret.deploy(wait=True)

            ocp_resource_provider = Provider(
                client=admin_client,
                name=name,
                namespace=mtv_namespace,
                secret_name=name,
                secret_namespace=mtv_namespace,
                url=source_provider_data_copy["api_url"],
                provider_type=source_provider_data_copy["type"],
                vddk_init_image=source_provider_data_copy.get("vddk_init_image"),
            )
            ocp_resource_provider.deploy(wait=True)
            ocp_resource_provider.wait_for_status(Provider.Status.READY, timeout=600)
            yield (
                mtv_provider_object(
                    cr=ocp_resource_provider, provider_api=_provider_client, provider_data=source_provider_data_copy
                ),
                customized_secret,
                ocp_resource_provider,
            )


def generate_time_based_uuid_name(name: str) -> str:
    return f"{name}-{datetime.now().strftime('%y-%d-%m-%H-%M-%S')}-{uuid.uuid4().hex[0:3]}"
