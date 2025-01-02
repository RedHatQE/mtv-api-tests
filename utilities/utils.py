import shortuuid
from contextlib import contextmanager
import copy
from pathlib import Path
import shutil
from subprocess import check_output, STDOUT
from time import sleep
from typing import Any, Generator, Optional, Tuple

from ocp_resources.exceptions import MissingResourceResError
from ocp_resources.provider import Provider
from ocp_resources.resource import DynamicClient
import pytest
from simple_logger.logger import get_logger

from ocp_resources.virtual_machine import VirtualMachine
from ocp_resources.secret import Secret
import threading

from libs.base_provider import BaseProvider
from libs.providers.cnv import CNVProvider
from libs.providers.ova import OVAProvider
from libs.providers.openstack import OpenStackProvider
from libs.providers.rhv import RHVProvider
from libs.providers.vmware import VMWareProvider

LOGGER = get_logger(__name__)


def get_guest_os_credentials(provider_data: dict[str, str], vm_dict: dict[str, str]) -> tuple[str, str]:
    win_os = vm_dict["win_os"]
    user = provider_data["guest_vm_win_user"] if win_os else provider_data["guest_vm_linux_user"]
    password = provider_data["guest_vm_win_password"] if win_os else provider_data["guest_vm_linux_password"]
    return user, password


def vmware_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == Provider.ProviderType.VSPHERE


def rhv_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == Provider.ProviderType.RHV


def openstack_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == "openstack"


def ova_provider(provider_data: dict[str, Any]) -> bool:
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


def is_true(value):
    if isinstance(value, str):
        return value.lower() in ["true", "1", "t", "y", "yes"]

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value == 1

    return False


def background(func):
    """
    a threading decorator
    use @background above the function you want to run in the background
    """

    def backgrnd_func(*args, **kwargs):
        threading.Thread(target=func, args=args, kwargs=kwargs).start()

    return backgrnd_func


def gen_network_map_list(
    source_provider_data: dict[str, Any],
    target_namespace: str,
    multus_network_name: str = "",
    pod_only: bool = False,
) -> list[dict[str, dict[str, str]]]:
    network_map_list: list[dict[str, dict[str, str]]] = []
    _destination_pod: dict[str, str] = {"type": "pod"}
    _destination_multus: dict[str, str] = {
        "name": multus_network_name,
        "namespace": target_namespace,
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
    return generate_name_with_uuid(name=name)


@contextmanager
def create_source_provider(
    config: dict[str, Any],
    source_provider_data: dict[str, Any],
    mtv_namespace: str,
    admin_client: DynamicClient,
    tmp_dir: Optional[pytest.TempPathFactory] = None,
    **kwargs: dict[str, Any],
) -> Generator[Tuple[BaseProvider, Secret | None, Provider | None], None, None]:
    # common
    source_provider: Any = None
    source_provider_data_copy = copy.deepcopy(source_provider_data)

    if config["source_provider_type"] == Provider.ProviderType.OPENSHIFT:
        provider = Provider(name="host", namespace=mtv_namespace, client=admin_client)
        if not provider.exists:
            raise MissingResourceResError(f"Provider {provider.name} not found")

        yield (
            CNVProvider(
                ocp_resource=provider,
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
        metadata_labels = {
            "createdForProviderType": source_provider_data_copy["type"],
        }
        # vsphere/vmware
        if vmware_provider(provider_data=source_provider_data_copy):
            provider_args["host"] = source_provider_data_copy["fqdn"]
            source_provider = VMWareProvider
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
            source_provider = RHVProvider
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
            source_provider = OpenStackProvider
            secret_string_data["username"] = source_provider_data_copy["username"]
            secret_string_data["password"] = source_provider_data_copy["password"]
            secret_string_data["regionName"] = source_provider_data_copy["region_name"]
            secret_string_data["projectName"] = source_provider_data_copy["project_name"]
            secret_string_data["domainName"] = source_provider_data_copy["user_domain_name"]

        elif ova_provider(provider_data=source_provider_data_copy):
            provider_args["host"] = source_provider_data_copy["api_url"]
            source_provider = OVAProvider

        secret_string_data["url"] = source_provider_data_copy["api_url"]
        secret_string_data["insecureSkipVerify"] = config["insecure_verify_skip"]

        if not source_provider:
            raise ValueError("Failed to get provider client")

        # this is for communication with the provider
        with source_provider(provider_data=source_provider_data_copy, **provider_args) as _source_provider:
            if not _source_provider.test:
                pytest.skip(f"Skipping VM import tests: {provider_args['host']} is not available.")

            # Creating the source Secret and source Provider CRs
            customized_secret = Secret(
                client=admin_client,
                name=name,
                namespace=mtv_namespace,
                string_data=secret_string_data,
                label=metadata_labels,
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
            _source_provider.ocp_resource = ocp_resource_provider
            yield _source_provider, customized_secret, ocp_resource_provider


@background
def start_source_vm_data_upload_vmware(provider_data, vm_names_list):
    provider_args = {
        "username": provider_data["username"],
        "password": provider_data["password"],
        "host": provider_data["fqdn"],
        "provider_data": provider_data,
    }
    print("start data generation")
    with VMWareProvider(**provider_args) as vmware_provider:
        vmware_provider.clear_vm_data(vm_names_list=vm_names_list)
        while vmware_provider.upload_data_to_vms(vm_names_list=vm_names_list):
            sleep(1)

        vmware_provider.disconnect()


def create_source_cnv_vm(dyn_client, vm_name, namespace):
    vm_file = f"{vm_name}.yaml"
    shutil.copyfile("tests/manifests/cnv-vm.yaml", vm_file)

    with open(vm_file, "r") as fd:
        content = fd.read()

    content = content.replace("vmname", vm_name)
    content = content.replace("vm-namespace", namespace)

    with open(vm_file, "w") as fd:
        fd.write(content)

    cnv_vm = VirtualMachine(client=dyn_client, yaml_file=vm_file, namespace=namespace)
    if not cnv_vm.exists:
        cnv_vm.deploy(wait=True)

    if not cnv_vm.ready:
        cnv_vm.start(wait=True)


def generate_name_with_uuid(name: str) -> str:
    return f"{name}-{shortuuid.ShortUUID().random(length=8).lower()}"
