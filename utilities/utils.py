import copy
import functools
import multiprocessing
import os
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from subprocess import STDOUT, check_output
from typing import Any

import pytest
from kubernetes.dynamic import DynamicClient
from ocp_resources.cluster_service_version import ClusterServiceVersion
from ocp_resources.data_source import DataSource
from ocp_resources.network_attachment_definition import NetworkAttachmentDefinition
from ocp_resources.provider import Provider
from ocp_resources.resource import get_client

# Optional import if available
from ocp_resources.secret import Secret
from ocp_resources.virtual_machine import VirtualMachine
from ocp_resources.virtual_machine_cluster_instancetype import VirtualMachineClusterInstancetype
from ocp_resources.virtual_machine_cluster_preference import VirtualMachineClusterPreference
from packaging.version import InvalidVersion, parse as version_parse
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
    return provider_data["type"] == Provider.ProviderType.OPENSTACK


def ova_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == Provider.ProviderType.OVA


def ocp_provider(provider_data: dict[str, Any]) -> bool:
    return provider_data["type"] == Provider.ProviderType.OPENSHIFT


def generate_ca_cert_file(provider_fqdn: str, cert_file: Path) -> Path:
    cert = check_output(
        [
            "/bin/sh",
            "-c",
            f"openssl s_client -connect {provider_fqdn}:443 -showcerts < /dev/null",
        ],
        stderr=STDOUT,
    )

    # Validate certificate data
    if b"BEGIN CERTIFICATE" not in cert:
        raise ValueError(f"Failed to download valid certificate from {provider_fqdn}")

    cert_file.write_bytes(cert)
    return cert_file


def _fetch_and_store_cacert(
    source_provider_data: dict[str, Any],
    secret_string_data: dict[str, Any],
    tmp_dir: pytest.TempPathFactory | None,
    session_uuid: str,
) -> Path:
    """
    Fetch CA certificate from provider and store in secret data.

    Args:
        source_provider_data: Provider configuration with 'type' and 'fqdn'
        secret_string_data: Secret data dict to add 'cacert' to
        tmp_dir: Temp directory factory for cert file
        session_uuid: Session UUID for unique filename

    Returns:
        Path to the certificate file

    Raises:
        ValueError: If tmp_dir is not provided
    """
    source_provider_type = source_provider_data["type"]
    if not tmp_dir:
        raise ValueError(f"tmp_dir is required for {source_provider_type} with SSL verification")

    cert_file = generate_ca_cert_file(
        provider_fqdn=source_provider_data["fqdn"],
        cert_file=tmp_dir.mktemp(source_provider_type.upper()) / f"{source_provider_type}_{session_uuid}_cert.crt",
    )
    secret_string_data["cacert"] = cert_file.read_text()
    return cert_file


def get_mtv_version(ocp_admin_client: "DynamicClient", namespace: str = "openshift-mtv") -> str:
    """
    Get the MTV operator version from the cluster.

    Args:
        ocp_admin_client: OpenShift admin client
        namespace: Namespace where MTV operator is installed (default: openshift-mtv)

    Returns:
        str: MTV operator version string

    Raises:
        RuntimeError: If MTV operator version cannot be determined
    """
    mtv_version: str | None = None

    for csv in ClusterServiceVersion.get(client=ocp_admin_client, namespace=namespace):
        if csv.instance and csv.instance.spec:
            display_name = csv.instance.spec.displayName
            version_str = csv.instance.spec.version

            if display_name and "Migration Toolkit for Virtualization" in display_name:
                mtv_version = version_str
                break

    if not mtv_version:
        raise RuntimeError("MTV operator version not found")

    LOGGER.info("Found MTV operator version: %s", mtv_version)
    return mtv_version


def has_mtv_minimum_version(min_version: str, client: "DynamicClient | None" = None) -> bool:
    """Check if MTV version is greater than or equal to the minimum version.

    Args:
        min_version (str): Minimum MTV version required (e.g., "2.10.0")
        client (DynamicClient | None): Optional OpenShift DynamicClient. If None, creates client internally.

    Returns:
        bool: True if MTV version >= min_version, False otherwise

    Raises:
        ValueError: If min_version or MTV version format is invalid
        RuntimeError: If MTV operator not found or cluster client creation fails
    """
    if client is None:
        try:
            client = get_cluster_client()
        except Exception:
            LOGGER.exception("Failed to create cluster client for MTV version check")
            raise RuntimeError("Could not create cluster client") from None

    mtv_version = get_mtv_version(client)

    # Parse and validate min_version first
    try:
        parsed_min_version = version_parse(min_version)
    except InvalidVersion as e:
        raise ValueError(f"Invalid min_version format '{min_version}': {e}") from e

    # Parse and validate mtv_version
    try:
        parsed_mtv_version = version_parse(mtv_version)
    except InvalidVersion as e:
        raise ValueError(f"Invalid MTV version format '{mtv_version}': {e}") from e

    return parsed_mtv_version >= parsed_min_version


def background(func):
    """Use @background above the function you want to run in the background"""

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
    multus_counter = 1

    for index, network in enumerate(source_provider_inventory.vms_networks_mappings(vms=vms)):
        if pod_only or index == 0:
            # First network or pod_only mode → pod network
            _destination = _destination_pod
        else:
            # Generate unique NAD name for each additional network
            if multus_network_name:
                # Use consistent naming: {base_name}-1, {base_name}-2, etc.
                # Where base_name includes unique test identifier (e.g., cnv-bridge-abc12345)
                nad_name = f"{multus_network_name}-{multus_counter}"
            else:
                # Default naming scheme
                nad_name = f"migration-nad-{multus_counter}"

            _destination = {
                "name": nad_name,
                "namespace": target_namespace,
                "type": "multus",
            }
            multus_counter += 1  # Increment for next NAD

        network_map_list.append({
            "destination": _destination,
            "source": network,
        })
    return network_map_list


@contextmanager
def create_source_provider(
    source_provider_data: dict[str, Any],
    namespace: str,
    admin_client: DynamicClient,
    session_uuid: str,
    fixture_store: dict[str, Any],
    ocp_admin_client: DynamicClient,
    destination_ocp_secret: Secret,
    insecure: bool,
    tmp_dir: pytest.TempPathFactory | None = None,
) -> Generator[BaseProvider, None, None]:
    # common
    source_provider_secret: Secret | None = None
    source_provider: Any = None
    source_provider_data_copy = copy.deepcopy(source_provider_data)

    # Check if copy-offload configuration is present
    has_copyoffload = "copyoffload" in source_provider_data_copy

    secret_string_data = {
        "url": source_provider_data_copy["api_url"],
        "insecureSkipVerify": "true" if insecure else "false",
    }
    provider_args = {
        "username": source_provider_data_copy["username"],
        "password": source_provider_data_copy["password"],
        "fixture_store": fixture_store,
    }
    metadata_labels = {
        "createdForProviderType": source_provider_data_copy["type"],
    }

    if ocp_provider(provider_data=source_provider_data_copy):
        source_provider = OCPProvider
        source_provider_data_copy["api_url"] = ocp_admin_client.configuration.host
        source_provider_data_copy["type"] = Provider.ProviderType.OPENSHIFT
        source_provider_secret = destination_ocp_secret

    elif vmware_provider(provider_data=source_provider_data_copy):
        source_provider = VMWareProvider
        provider_args["host"] = source_provider_data_copy["fqdn"]
        secret_string_data["user"] = source_provider_data_copy["username"]
        secret_string_data["password"] = source_provider_data_copy["password"]
        # Pass copyoffload configuration if present
        if has_copyoffload:
            provider_args["copyoffload"] = source_provider_data_copy["copyoffload"]

        if not insecure:
            _fetch_and_store_cacert(source_provider_data_copy, secret_string_data, tmp_dir, session_uuid)

    elif rhv_provider(provider_data=source_provider_data_copy):
        source_provider = OvirtProvider
        provider_args["host"] = source_provider_data_copy["api_url"]
        secret_string_data["user"] = source_provider_data_copy["username"]
        secret_string_data["password"] = source_provider_data_copy["password"]

        # Always fetch CA certificate for RHV provider, even when insecure=True
        # The certificate is required for imageio connection, insecureSkipVerify controls validation
        cert_file = _fetch_and_store_cacert(source_provider_data_copy, secret_string_data, tmp_dir, session_uuid)

        # Set ca_file in provider_args only when secure mode (for SDK connection)
        if not insecure:
            provider_args["ca_file"] = str(cert_file)
        else:
            provider_args["insecure"] = insecure

    elif openstack_provider(provider_data=source_provider_data_copy):
        source_provider = OpenStackProvider
        provider_args["host"] = source_provider_data_copy["api_url"]
        provider_args["auth_url"] = source_provider_data_copy["api_url"]
        provider_args["project_name"] = source_provider_data_copy["project_name"]
        provider_args["user_domain_name"] = source_provider_data_copy["user_domain_name"]
        provider_args["region_name"] = source_provider_data_copy["region_name"]
        provider_args["user_domain_id"] = source_provider_data_copy["user_domain_id"]
        provider_args["project_domain_id"] = source_provider_data_copy["project_domain_id"]
        secret_string_data["username"] = source_provider_data_copy["username"]
        secret_string_data["password"] = source_provider_data_copy["password"]
        secret_string_data["regionName"] = source_provider_data_copy["region_name"]
        secret_string_data["projectName"] = source_provider_data_copy["project_name"]
        secret_string_data["domainName"] = source_provider_data_copy["user_domain_name"]

        # Add CA certificate for SSL verification
        if not insecure:
            _fetch_and_store_cacert(source_provider_data_copy, secret_string_data, tmp_dir, session_uuid)

    elif ova_provider(provider_data=source_provider_data_copy):
        source_provider = OVAProvider
        provider_args["host"] = source_provider_data_copy["api_url"]

    if not source_provider:
        raise ValueError("Failed to get source provider data")

    if not source_provider_secret:  # OCP provider use the local OCP secret
        # Creating the source Secret and source Provider CRs
        source_provider_secret = create_and_store_resource(
            fixture_store=fixture_store,
            resource=Secret,
            client=admin_client,
            namespace=namespace,
            string_data=secret_string_data,
            label=metadata_labels,
        )

    if not source_provider_secret:
        raise ValueError("Failed to create source provider secret")

    # Add copy-offload annotation only when copy-offload is configured
    provider_annotations = {}
    if vmware_provider(provider_data=source_provider_data_copy) and has_copyoffload:
        provider_annotations["forklift.konveyor.io/empty-vddk-init-image"] = "yes"

    ocp_resource_provider = create_and_store_resource(
        fixture_store=fixture_store,
        resource=Provider,
        client=admin_client,
        namespace=namespace,
        secret_name=source_provider_secret.name,
        secret_namespace=namespace,
        url=source_provider_data_copy["api_url"],
        provider_type=source_provider_data_copy["type"],
        vddk_init_image=source_provider_data_copy.get("vddk_init_image"),
        annotations=provider_annotations or None,
    )
    ocp_resource_provider.wait_for_status(Provider.Status.READY, timeout=600, stop_status="ConnectionFailed")

    # this is for communication with the provider
    with source_provider(ocp_resource=ocp_resource_provider, **provider_args) as _source_provider:
        if not _source_provider.test:
            pytest.fail(f"{source_provider.type} provider {provider_args['host']} is not available.")

        yield _source_provider


def create_source_cnv_vms(
    fixture_store: dict[str, Any],
    client: DynamicClient,
    vms: list[dict[str, Any]],
    namespace: str,
    network_name: str,
    vm_name_suffix: str,
) -> None:
    vms_to_create: list[VirtualMachine] = []

    for vm_dict in vms:
        vms_to_create.append(
            create_and_store_resource(
                resource=VirtualMachineFromInstanceType,
                fixture_store=fixture_store,
                name=f"{vm_dict['name']}{vm_name_suffix}",
                namespace=namespace,
                client=client,
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
            ),
        )

    for vm in vms_to_create:
        if not vm.ready:
            vm.start()

    for vm in vms_to_create:
        vm.wait_for_ready_status(status=True)


def get_value_from_py_config(value: str) -> Any:
    config_value = py_config.get(value)

    if not config_value:
        return config_value

    if isinstance(config_value, str):
        if config_value.lower() == "true":
            return True

        if config_value.lower() == "false":
            return False

        return config_value

    return config_value


def delete_all_vms(ocp_admin_client: DynamicClient, namespace: str) -> None:
    for vm in VirtualMachine.get(client=ocp_admin_client, namespace=namespace):
        with suppress(Exception):
            vm.clean_up(wait=True)


class VirtualMachineFromInstanceType(VirtualMachine):
    """Custom VirtualMachine class that simplifies VM creation with instancetype/preference
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
        """Initialize VirtualMachineFromInstanceType with automatic configuration

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
        client = kwargs.get("client")
        kwargs.setdefault("client", client)

        super().__init__(**kwargs)

        # Create instancetype object - required
        self.instancetype: VirtualMachineClusterInstancetype = VirtualMachineClusterInstancetype(
            client=client,
            name=instancetype_name,
        )

        # Create preference object - required
        self.preference: VirtualMachineClusterPreference = VirtualMachineClusterPreference(
            client=client,
            name=preference_name,
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


def get_cluster_client() -> DynamicClient:
    host = get_value_from_py_config("cluster_host")
    username = get_value_from_py_config("cluster_username")
    password = get_value_from_py_config("cluster_password")
    insecure_verify_skip = get_value_from_py_config("insecure_verify_skip")
    _client = get_client(host=host, username=username, password=password, verify_ssl=not insecure_verify_skip)

    if isinstance(_client, DynamicClient):
        return _client
    raise ValueError("Failed to get client for cluster")
