import copy
import functools
import multiprocessing
import shlex
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from subprocess import STDOUT, check_output
from time import sleep
from typing import Any, Generator, Tuple

import pytest
import shortuuid
from kubernetes.dynamic import DynamicClient
from ocp_resources.exceptions import MissingResourceResError
from ocp_resources.provider import Provider
from ocp_resources.resource import NamespacedResource, Resource
from ocp_resources.secret import Secret
from ocp_resources.virtual_machine import VirtualMachine
from ocp_utilities.monitoring import Prometheus
from pyhelper_utils.shell import run_command
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider
from libs.providers.cnv import CNVProvider
from libs.providers.openstack import OpenStackProvider
from libs.providers.ova import OVAProvider
from libs.providers.rhv import OvirtProvider
from libs.providers.vmware import VMWareProvider
from utilities.resources import create_and_store_resource

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


def provider_cr_name(session_uuid: str, provider_data: dict[str, Any], username: str) -> str:
    return (
        f"{session_uuid}-{provider_data['type']}-{provider_data['version'].replace('.', '-')}-"
        f"{provider_data['fqdn'].split('.')[0]}-{username.split('@')[0]}"
    )


@contextmanager
def create_source_provider(
    config: dict[str, Any],
    source_provider_data: dict[str, Any],
    mtv_namespace: str,
    admin_client: DynamicClient,
    session_uuid: str,
    fixture_store: dict[str, Any],
    tmp_dir: pytest.TempPathFactory | None = None,
    **kwargs: dict[str, Any],
) -> Generator[Tuple[BaseProvider, Resource | NamespacedResource | None | None], None, None]:
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
        )

    else:
        for key, value in kwargs.items():
            source_provider_data_copy[key] = value

        name = provider_cr_name(
            session_uuid=session_uuid,
            provider_data=source_provider_data_copy,
            username=source_provider_data_copy["username"],
        )

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
        customized_secret = Secret(name=name, namespace=mtv_namespace, client=admin_client)

        if not customized_secret.exists:
            customized_secret = create_and_store_resource(
                fixture_store=fixture_store,
                session_uuid=session_uuid,
                resource=Secret,
                client=admin_client,
                name=name,
                namespace=mtv_namespace,
                string_data=secret_string_data,
                label=metadata_labels,
            )

        ocp_resource_provider = Provider(name=name, namespace=mtv_namespace, client=admin_client)

        if not ocp_resource_provider.exists:
            ocp_resource_provider = create_and_store_resource(
                fixture_store=fixture_store,
                session_uuid=session_uuid,
                resource=Provider,
                client=admin_client,
                name=name,
                namespace=mtv_namespace,
                secret_name=name,
                secret_namespace=mtv_namespace,
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

            yield _source_provider, customized_secret


@background
def start_source_vm_data_upload_vmware(vmware_provider: VMWareProvider, vm_names_list: list[str]) -> None:
    print("start data generation")
    vmware_provider.clear_vm_data(vm_names_list=vm_names_list)
    while vmware_provider.upload_data_to_vms(vm_names_list=vm_names_list):
        sleep(1)


def create_source_cnv_vm(dyn_client: DynamicClient, vm_name: str, namespace: str) -> None:
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


def get_source_provider_data() -> dict[str, Any]:
    _source_provider_type = py_config["source_provider_type"]
    _source_provider_version = py_config["source_provider_version"]

    _source_provider = [
        _provider
        for _provider in py_config["source_providers_list"]
        if _provider["type"] == _source_provider_type
        and _provider["version"] == _source_provider_version
        and _provider["default"] == "True"
    ]

    return _source_provider[0]


def prometheus_monitor_deamon(ocp_admin_client: DynamicClient) -> None:
    token_command = "oc create token prometheus-k8s -n openshift-monitoring --duration=999999s"
    _, token, _ = run_command(command=shlex.split(token_command), verify_stderr=False)
    prometheus = Prometheus(client=ocp_admin_client, verify_ssl=False, bearer_token=token)
    alerts_to_get: list[str] = ["CephOSDCriticallyFull", "CephClusterErrorState", "CephClusterReadOnly"]
    while True:
        for _alert in alerts_to_get:
            alerts = prometheus.get_firing_alerts(alert_name=_alert)
            if alerts:
                last_alert = alerts[0]
                annotations = last_alert["annotations"]
                severity = annotations["severity_level"]
                description = annotations["description"]
                message = annotations["message"]

                LOGGER.warning(f"{_alert}: {severity} - {message} - {description}")
        time.sleep(60)
