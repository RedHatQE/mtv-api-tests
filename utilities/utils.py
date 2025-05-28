import copy
import functools
import multiprocessing
from contextlib import contextmanager
from pathlib import Path
from subprocess import STDOUT, check_output
from typing import Any, Generator

import pytest
import shortuuid
import yaml
from kubernetes.dynamic import DynamicClient
from ocp_resources.provider import Provider
from ocp_resources.secret import Secret
from ocp_resources.virtual_machine import VirtualMachine
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.openshift import OCPProvider
from libs.providers.openstack import OpenStackProvider
from libs.providers.ova import OVAProvider
from libs.providers.rhv import OvirtProvider
from libs.providers.vmware import VMWareProvider
from utilities.resources import create_and_store_resource

LOGGER = get_logger(__name__)


def vmware_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == Provider.ProviderType.VSPHERE


def rhv_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == Provider.ProviderType.RHV


def openstack_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == "openstack"


def ova_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == "ova"


def generate_ca_cert_file(provider_fqdn: dict[str, Any], cert_file: Path) -> Path:
    cert = check_output(
        [
            "/bin/sh",
            "-c",
            f"openssl s_client -connect {provider_fqdn}:443 -showcerts < /dev/null",
        ],
        stderr=STDOUT,
    )

    cert_file.write_bytes(cert)
    return cert_file


def background(func):
    """
    use @background above the function you want to run in the background
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        proc = multiprocessing.Process(target=func, args=args, kwargs=kwargs)
        proc.start()

    return wrapper


def gen_network_map_list(
    source_provider_inventory: ForkliftInventory,
    target_namespace: str,
    vms: list[str],
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

    for index, network in enumerate(source_provider_inventory.vms_networks_mappings(vms=vms)):
        if not pod_only:
            if index == 0:
                _destination = _destination_pod
            else:
                _destination = _destination_multus

        network_map_list.append({
            "destination": _destination,
            "source": network,
        })
    return network_map_list


def generated_provider_name(session_uuid: str, provider_data: dict[str, Any]) -> str:
    _name = (
        f"{session_uuid}-{provider_data['type']}-{provider_data['version'].replace('.', '-')}-"
        f"{provider_data['fqdn'].split('.')[0]}-{provider_data['username'].split('@')[0]}"
    )
    return generate_name_with_uuid(name=_name)


@contextmanager
def create_source_provider(
    config: dict[str, Any],
    source_provider_data: dict[str, Any],
    namespace: str,
    admin_client: DynamicClient,
    session_uuid: str,
    fixture_store: dict[str, Any],
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    destination_ocp_secret: Secret,
    tmp_dir: pytest.TempPathFactory | None = None,
    **kwargs: dict[str, Any],
) -> Generator[BaseProvider, None, None]:
    # common
    source_provider: Any = None
    source_provider_data_copy = copy.deepcopy(source_provider_data)

    source_provider_name = generated_provider_name(
        session_uuid=session_uuid,
        provider_data=source_provider_data_copy,
    )

    if config["source_provider_type"] == Provider.ProviderType.OPENSHIFT:
        provider = create_and_store_resource(
            fixture_store=fixture_store,
            session_uuid=session_uuid,
            resource=Provider,
            name=source_provider_name,
            namespace=target_namespace,
            secret_name=destination_ocp_secret.name,
            secret_namespace=destination_ocp_secret.namespace,
            url=ocp_admin_client.configuration.host,
            provider_type=Provider.ProviderType.OPENSHIFT,
        )

        yield OCPProvider(
            ocp_resource=provider,
            provider_data=source_provider_data_copy,
        )

    else:
        for key, value in kwargs.items():
            source_provider_data_copy[key] = value

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
                provider_fqdn=source_provider_data_copy["fqdn"],
                cert_file=tmp_dir.mktemp(source_provider_data_copy["type"].upper())
                / f"{source_provider_data_copy['type']}_cert.crt",
            )
            provider_args["host"] = source_provider_data_copy["api_url"]
            provider_args["ca_file"] = str(cert_file)
            source_provider = OvirtProvider
            secret_string_data["user"] = source_provider_data_copy["username"]
            secret_string_data["password"] = source_provider_data_copy["password"]
            secret_string_data["cacert"] = cert_file.read_text()

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
            raise ValueError("Failed to get source provider data")

        # Creating the source Secret and source Provider CRs
        source_provider_secret = create_and_store_resource(
            fixture_store=fixture_store,
            session_uuid=session_uuid,
            resource=Secret,
            client=admin_client,
            name=generate_name_with_uuid(name=source_provider_name),
            namespace=namespace,
            string_data=secret_string_data,
            label=metadata_labels,
        )

        ocp_resource_provider = create_and_store_resource(
            fixture_store=fixture_store,
            session_uuid=session_uuid,
            resource=Provider,
            client=admin_client,
            name=source_provider_name,
            namespace=namespace,
            secret_name=source_provider_secret.name,
            secret_namespace=namespace,
            url=source_provider_data_copy["api_url"],
            provider_type=source_provider_data_copy["type"],
            vddk_init_image=source_provider_data_copy.get("vddk_init_image"),
        )
        ocp_resource_provider.wait_for_status(Provider.Status.READY, timeout=600)

        # this is for communication with the provider
        with source_provider(
            provider_data=source_provider_data_copy, ocp_resource=ocp_resource_provider, **provider_args
        ) as _source_provider:
            if not _source_provider.test:
                pytest.skip(f"Skipping VM import tests: {provider_args['host']} is not available.")

            yield _source_provider


def create_source_cnv_vms(
    fixture_store: Any,
    dyn_client: DynamicClient,
    vms: list[dict[str, Any]],
    namespace: str,
    network_name: str,
) -> None:
    vms_to_create: list[VirtualMachine] = []

    for vm_dict in vms:
        with open("tests/manifests/cnv-vm.yaml", "r") as fd:
            content = fd.read()

        content = content.replace("vmname", vm_dict["name"])
        content = content.replace("vm-namespace", namespace)
        content = content.replace("mybridge", network_name)

        yaml_dict = yaml.safe_load(content)

        cnv_vm = VirtualMachine(client=dyn_client, kind_dict=yaml_dict, namespace=namespace)

        # Needed to build the resource body
        cnv_vm.to_dict()

        if not cnv_vm.exists:
            cnv_vm.deploy()
            LOGGER.info(f"Storing {cnv_vm.kind} {cnv_vm.name} in fixture store")
            _resource_dict = {"name": cnv_vm.name, "namespace": cnv_vm.namespace, "module": VirtualMachine.__module__}
            fixture_store["teardown"].setdefault(VirtualMachine, []).append(_resource_dict)

        vms_to_create.append(cnv_vm)

    for vm in vms_to_create:
        if not vm.ready:
            vm.start()

    for vm in vms_to_create:
        vm.wait_for_ready_status(status=True)


def generate_name_with_uuid(name: str) -> str:
    _name = f"{name}-{shortuuid.ShortUUID().random(length=4).lower()}"
    _name = _name.replace("_", "-").replace(".", "-").lower()
    return _name


def get_value_from_py_config(value: str) -> Any:
    config_value = py_config.get(value)

    if not config_value:
        return config_value

    if isinstance(config_value, str):
        if config_value.lower() == "true":
            return True

        elif config_value.lower() == "false":
            return False

        else:
            return config_value

    else:
        return config_value
