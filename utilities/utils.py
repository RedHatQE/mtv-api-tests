import copy
import functools
import multiprocessing
from contextlib import contextmanager, suppress
from pathlib import Path
from subprocess import STDOUT, check_output
from typing import Any, Generator

import pytest
import shortuuid
from kubernetes.dynamic import DynamicClient
from ocp_resources.data_source import DataSource
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.provider import Provider

# Optional import if available
from ocp_resources.resource import get_client
from ocp_resources.secret import Secret
from ocp_resources.virtual_machine import VirtualMachine
from ocp_resources.virtual_machine_cluster_instancetype import VirtualMachineClusterInstancetype
from ocp_resources.virtual_machine_cluster_preference import VirtualMachineClusterPreference
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
    insecure: bool,
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
            if not insecure:
                if not tmp_dir:
                    raise ValueError("tmp_dir is required for rhv")

                source_provider_type = source_provider_data_copy["type"]
                cert_file = generate_ca_cert_file(
                    provider_fqdn=source_provider_data_copy["fqdn"],
                    cert_file=tmp_dir.mktemp(source_provider_type.upper())
                    / f"{source_provider_type}_{session_uuid}_cert.crt",
                )
                provider_args["ca_file"] = str(cert_file)
                secret_string_data["cacert"] = cert_file.read_text()

            else:
                provider_args["insecure"] = insecure

            provider_args["host"] = source_provider_data_copy["api_url"]
            source_provider = OvirtProvider
            secret_string_data["user"] = source_provider_data_copy["username"]
            secret_string_data["password"] = source_provider_data_copy["password"]

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
    fixture_store: dict[str, Any],
    dyn_client: DynamicClient,
    vms: list[dict[str, Any]],
    namespace: str,
    network_name: str,
    session_uuid: str,
) -> None:
    vms_to_create: list[VirtualMachine] = []

    for vm_dict in vms:
        vms_to_create.append(
            create_and_store_resource(
                resource=VirtualMachineFromInstanceType,
                fixture_store=fixture_store,
                session_uuid=session_uuid,
                name=vm_dict["name"],
                namespace=namespace,
                client=get_client(),
                instancetype_name="u1.small",
                preference_name="rhel.9",
                datasource_name="rhel9",
                storage_size="30Gi",
                additional_networks=[network_name],
                cloud_init_user_data="""#cloud-config
chpasswd:
expire: false
password: 123456
user: rhel
""",
                run_strategy=VirtualMachine.RunStrategy.MANUAL,
            )
        )
        # with open("tests/manifests/cnv-vm.yaml", "r") as fd:
        #     content = fd.read()
        #
        # content = content.replace("vmname", vm_dict["name"])
        # content = content.replace("vm-namespace", namespace)
        # content = content.replace("mybridge", network_name)
        #
        # yaml_dict = yaml.safe_load(content)
        #
        # cnv_vm = VirtualMachine(client=dyn_client, kind_dict=yaml_dict, namespace=namespace)
        #
        # # Needed to build the resource body
        # cnv_vm.to_dict()
        #
        # if not cnv_vm.exists:
        #     cnv_vm.deploy()
        #     LOGGER.info(f"Storing {cnv_vm.kind} {cnv_vm.name} in fixture store")
        #     _resource_dict = {"name": cnv_vm.name, "namespace": cnv_vm.namespace, "module": VirtualMachine.__module__}
        #     fixture_store["teardown"].setdefault(VirtualMachine, []).append(_resource_dict)
        #
        # vms_to_create.append(cnv_vm)

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


def delete_all_vms(ocp_admin_client: DynamicClient, namespace: str) -> None:
    for vm in VirtualMachine.get(dyn_client=ocp_admin_client, namespace=namespace):
        with suppress(Exception):
            vm.clean_up(wait=True)


class VirtualMachineFromInstanceType(VirtualMachine):
    """
    Custom VirtualMachine class that simplifies VM creation with instancetype/preference
    and automatically builds the entire configuration from simple parameters.
    """

    def __init__(
        self,
        instancetype_name: str,
        preference_name: str,
        datasource_name: str | None = None,
        datasource_namespace: str = "openshift-virtualization-os-images",
        storage_size: str = "30Gi",
        additional_networks: list[str] | None = None,  # List of NAD names for multus networks
        cloud_init_user_data: str | None = None,
        run_strategy: str = VirtualMachine.RunStrategy.MANUAL,
        labels: dict[str, str] | None = None,
        annotations: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        """
        Initialize VirtualMachineFromInstanceType with automatic configuration

        Args:
            instancetype_name: Name of the cluster instancetype (e.g., "u1.small")
            preference_name: Name of the cluster preference (e.g., "rhel.9")
            datasource_name: Name of the DataSource to use for root disk
            datasource_namespace: Namespace of the DataSource (default: openshift-virtualization-os-images)
            storage_size: Size of the root disk (default: 30Gi)
            additional_networks: List of NetworkAttachmentDefinition names to add as multus networks
            cloud_init_user_data: Cloud-init user data (e.g., for setting password)
            run_strategy: VM run strategy (default: Manual)
            labels: Labels for the VM template
            annotations: Annotations for the VM
            **kwargs: Additional arguments passed to the base VirtualMachine class (name, namespace, client, etc.)
        """
        # Extract client from kwargs to use with resource creation before calling super()
        client = kwargs.get("client") or get_client()
        kwargs.setdefault("client", client)

        super().__init__(**kwargs)

        # Create instancetype object - required
        self.instancetype: VirtualMachineClusterInstancetype = VirtualMachineClusterInstancetype(
            client=client, name=instancetype_name
        )

        # Create preference object - required
        self.preference: VirtualMachineClusterPreference = VirtualMachineClusterPreference(
            client=client, name=preference_name
        )

        # Store configuration
        self.run_strategy = run_strategy
        self.datasource_name = datasource_name
        self.datasource_namespace = datasource_namespace
        self.storage_size = storage_size
        self.additional_networks = additional_networks or []
        self.cloud_init_user_data = cloud_init_user_data
        self.vm_labels = labels or {}
        self.annotations = annotations or {}

        # Initialize lists for VM components - will be populated in to_dict() if needed
        self.data_volume_templates: list[dict[str, Any]] = []
        self.volumes: list[dict[str, Any]] = []
        self.networks: list[dict[str, Any]] = []
        self.interfaces: list[dict[str, Any]] = []

    def _build_vm_configuration(self) -> None:
        """Build the complete VM configuration from the provided parameters"""
        # Add DataSource-based disk if datasource_name is provided
        if self.datasource_name:
            # Create the DataSource object
            datasource = DataSource(client=self.client, name=self.datasource_name, namespace=self.datasource_namespace)

            # Add DataVolumeTemplate
            dv_name = self.name
            self.data_volume_templates.append({
                "metadata": {"name": dv_name},
                "spec": {
                    "sourceRef": {"kind": "DataSource", "name": datasource.name, "namespace": datasource.namespace},
                    "storage": {"resources": {"requests": {"storage": self.storage_size}}},
                },
            })

            # Add volume referencing the DataVolume
            self.volumes.append({"name": "rootdisk", "dataVolume": {"name": dv_name}})

        # Add cloud-init volume if provided
        if self.cloud_init_user_data:
            self.volumes.append({"name": "cloudinit", "cloudInitNoCloud": {"userData": self.cloud_init_user_data}})

        # Add default pod network
        self.networks.append({"name": "default", "pod": {}})
        self.interfaces.append({"name": "default", "masquerade": {}, "model": "virtio"})

        # Add additional multus networks
        for i, nad_name in enumerate(self.additional_networks):
            network_name = f"net{i + 1}"
            nad = NetworkAttachmentDefinition(client=self.client, name=nad_name, namespace=self.namespace)

            self.networks.append({"name": network_name, "multus": {"networkName": f"{nad.namespace}/{nad.name}"}})

            self.interfaces.append({"name": network_name, "bridge": {}, "model": "virtio"})

    def to_dict(self) -> None:
        """Build the VM specification"""
        super().to_dict()

        # If there's no kind_dict and no yaml_file, build it
        if not self.kind_dict and not self.yaml_file:
            # Build the VM configuration only when needed
            self._build_vm_configuration()
            # Build spec
            spec: dict[str, Any] = {}

            # Add dataVolumeTemplates if provided
            if self.data_volume_templates:
                spec["dataVolumeTemplates"] = self.data_volume_templates

            # Add instancetype reference - required
            if not self.instancetype.name:
                raise ValueError("VirtualMachineClusterInstancetype must have a name")
            spec["instancetype"] = {"kind": "VirtualMachineClusterInstancetype", "name": self.instancetype.name}

            # Add preference reference - required
            if not self.preference.name:
                raise ValueError("VirtualMachineClusterPreference must have a name")
            spec["preference"] = {"kind": "VirtualMachineClusterPreference", "name": self.preference.name}

            # Set run strategy
            spec["runStrategy"] = self.run_strategy

            # Build template
            template: dict[str, Any] = {"metadata": {}, "spec": {"domain": {"devices": {}}}}

            # Add labels to template metadata
            if self.vm_labels:
                template["metadata"]["labels"] = self.vm_labels

            # Add resources (empty for instancetype)
            template["spec"]["domain"]["resources"] = {}

            # Add interfaces
            if self.interfaces:
                template["spec"]["domain"]["devices"]["interfaces"] = self.interfaces

            # Add networks
            if self.networks:
                template["spec"]["networks"] = self.networks

            # Add volumes (already built in _build_vm_configuration)
            if self.volumes:
                template["spec"]["volumes"] = self.volumes

            # Set template in spec
            spec["template"] = template

            # Set the complete spec
            self.res["spec"] = spec
