from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.provider import Provider
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider
from libs.providers.vmware import VMWareProvider
from utilities.copyoffload_constants import SUPPORTED_VENDORS
from utilities.copyoffload_migration import get_copyoffload_credential, wait_for_vmware_cloud_init_all_vms
from utilities.esxi import install_ssh_key_on_esxi, remove_ssh_key_from_esxi
from utilities.resources import create_and_store_resource
from utilities.utils import resolve_providers_json_path

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)


@pytest.fixture(scope="session")
def copyoffload_config(
    source_provider: BaseProvider,
    source_provider_data: dict[str, Any],
    request: pytest.FixtureRequest,
) -> None:
    """
    Validate copy-offload configuration before running copy-offload tests.

    This fixture performs all necessary validations:
    - Verifies vSphere provider type
    - Checks for copyoffload configuration
    - Validates storage credentials availability

    If any validation fails, the test will fail early with a clear error message.
    """
    providers_path = resolve_providers_json_path(cli_path=request.config.getoption("providers_json"))

    # Validate that this is a vSphere provider
    if source_provider.type != Provider.ProviderType.VSPHERE:
        pytest.fail(
            f"Copy-offload tests require vSphere provider, but got '{source_provider.type}'. "
            f"Check your provider configuration in {providers_path}"
        )

    # Validate copy-offload configuration exists
    if "copyoffload" not in source_provider_data:
        pytest.fail(
            "Copy-offload configuration not found in source provider data. "
            f"Add 'copyoffload' section to your provider in {providers_path}"
        )

    config = source_provider_data["copyoffload"]

    # Validate required storage credentials are available (from either env vars or providers JSON)
    required_credentials = ["storage_hostname", "storage_username", "storage_password"]
    missing_credentials = []

    for cred in required_credentials:
        # Check if credential is available from either env var or config file
        if not get_copyoffload_credential(cred, config):
            missing_credentials.append(cred)

    if missing_credentials:
        pytest.fail(
            f"Required storage credentials not found: {missing_credentials}. "
            f"Add them to {providers_path} copyoffload section or set environment variables: "
            f"{', '.join([f'COPYOFFLOAD_{c.upper()}' for c in missing_credentials])}"
        )

    # Validate required copy-offload parameters
    required_params = ["storage_vendor_product", "datastore_id"]
    missing_params = [param for param in required_params if not config.get(param)]

    if missing_params:
        raise ValueError(
            f"Missing required copy-offload parameters in config: {', '.join(missing_params)}. "
            f"Add them to {providers_path} copyoffload section"
        )

    LOGGER.info("✓ Copy-offload configuration validated successfully")


@pytest.fixture(scope="class")
def mixed_datastore_config(source_provider_data: dict[str, Any]) -> None:
    """Validate mixed datastore configuration for TestCopyoffloadMixedDatastoreMigration.

    Args:
        source_provider_data (dict[str, Any]): Source provider configuration data.

    Returns:
        None

    Raises:
        ValueError: If non_xcopy_datastore_id is missing.
    """
    copyoffload_config_data: dict[str, Any] = source_provider_data.get("copyoffload", {})
    non_xcopy_datastore_id: str | None = copyoffload_config_data.get("non_xcopy_datastore_id")

    if not non_xcopy_datastore_id:
        raise ValueError(
            "Mixed datastore test requires 'non_xcopy_datastore_id' to be configured in copyoffload section. "
            "This should be a datastore that does NOT support XCOPY."
        )

    LOGGER.info(f"✓ Mixed datastore configuration validated: non_xcopy_datastore_id = {non_xcopy_datastore_id}")


@pytest.fixture(scope="session")
def copyoffload_storage_secret(
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    source_provider_data: dict[str, Any],
    copyoffload_config: None,
    request: pytest.FixtureRequest,
) -> Secret:
    """
    Create a storage secret for copy-offload functionality.

    This fixture creates the storage secret required for copy-offload migrations
    with credentials from environment variables or providers JSON file.

    Args:
        fixture_store: Pytest fixture store for resource tracking
        ocp_admin_client: OpenShift admin client
        target_namespace: Target namespace for the secret
        source_provider_data: Source provider configuration data
        copyoffload_config: Copy-offload configuration (validates prerequisites)
        request: Pytest request object to access CLI options

    Returns:
        Secret: Created storage secret resource
    """
    LOGGER.info("Creating copy-offload storage secret")
    providers_path = resolve_providers_json_path(cli_path=request.config.getoption("providers_json"))

    copyoffload_cfg = source_provider_data["copyoffload"]

    # Get storage credentials from environment variables or provider config
    storage_hostname = get_copyoffload_credential("storage_hostname", copyoffload_cfg)
    storage_username = get_copyoffload_credential("storage_username", copyoffload_cfg)
    storage_password = get_copyoffload_credential("storage_password", copyoffload_cfg)

    if not all([storage_hostname, storage_username, storage_password]):
        raise ValueError(
            "Storage credentials are required. Set COPYOFFLOAD_STORAGE_HOSTNAME, COPYOFFLOAD_STORAGE_USERNAME, "
            f"and COPYOFFLOAD_STORAGE_PASSWORD environment variables or include them in {providers_path}"
        )

    # Validate storage vendor product
    storage_vendor = copyoffload_cfg.get("storage_vendor_product")
    if not storage_vendor:
        raise ValueError(
            f"storage_vendor_product is required in copyoffload configuration. "
            f"Valid values: {', '.join(SUPPORTED_VENDORS)}"
        )
    if storage_vendor not in SUPPORTED_VENDORS:
        LOGGER.warning(
            "storage_vendor_product '%s' is not in the list of known vendors: %s. "
            "Continuing anyway, but this may cause issues if the vendor is not supported by the populator.",
            storage_vendor,
            SUPPORTED_VENDORS,
        )

    # Base secret data (required for all vendors)
    secret_data = {
        "STORAGE_HOSTNAME": storage_hostname,
        "STORAGE_USERNAME": storage_username,
        "STORAGE_PASSWORD": storage_password,
    }

    # Vendor-specific configuration mapping
    # Maps vendor name to list of (config_key, secret_key, required) tuples
    # Based on forklift vsphere-xcopy-volume-populator code and README
    # NOTE: Keys must match SUPPORTED_VENDORS constant defined at module level
    vendor_specific_fields = {
        "ontap": [("ontap_svm", "ONTAP_SVM", True)],
        "vantara": [
            ("vantara_storage_id", "STORAGE_ID", True),
            ("vantara_storage_port", "STORAGE_PORT", True),
            ("vantara_hostgroup_id_list", "HOSTGROUP_ID_LIST", True),
        ],
        "primera3par": [],  # Only basic credentials required
        "pureFlashArray": [("pure_cluster_prefix", "PURE_CLUSTER_PREFIX", True)],
        "powerflex": [("powerflex_system_id", "POWERFLEX_SYSTEM_ID", True)],
        "powermax": [("powermax_symmetrix_id", "POWERMAX_SYMMETRIX_ID", True)],
        "powerstore": [],  # Only basic credentials required
        "infinibox": [],  # Only basic credentials required
        "flashsystem": [],  # Only basic credentials required
    }

    # Ensure vendor_specific_fields keys match SUPPORTED_VENDORS to prevent drift
    assert set(vendor_specific_fields.keys()) == set(SUPPORTED_VENDORS), (
        f"vendor_specific_fields keys must match SUPPORTED_VENDORS. "
        f"Missing in vendor_specific_fields: {set(SUPPORTED_VENDORS) - set(vendor_specific_fields.keys())}. "
        f"Extra in vendor_specific_fields: {set(vendor_specific_fields.keys()) - set(SUPPORTED_VENDORS)}"
    )

    # Add vendor-specific fields if configured
    if storage_vendor in vendor_specific_fields:
        for config_key, secret_key, required in vendor_specific_fields[storage_vendor]:
            value = get_copyoffload_credential(config_key, copyoffload_cfg)
            if value:
                secret_data[secret_key] = value
                LOGGER.info("✓ Added vendor-specific field: %s", secret_key)
            elif required:
                env_var_name = f"COPYOFFLOAD_{config_key.upper()}"
                raise ValueError(
                    f"Required vendor-specific field '{config_key}' not found for vendor '{storage_vendor}'. "
                    f"Add it to {providers_path} copyoffload section or set environment variable: {env_var_name}"
                )

    LOGGER.info("Creating storage secret for copy-offload with vendor: %s", storage_vendor)

    storage_secret = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        namespace=target_namespace,
        string_data=secret_data,
    )

    LOGGER.info("✓ Copy-offload storage secret created: %s", storage_secret.name)
    return storage_secret


@pytest.fixture(scope="session")
def setup_copyoffload_ssh(source_provider, source_provider_data, copyoffload_config):
    """
    Sets up SSH key on ESXi host for copy-offload if SSH method is enabled.

    Depends on copyoffload_config to ensure validation runs first.
    """
    copyoffload_cfg = source_provider_data["copyoffload"]  # Safe: copyoffload_config validates this exists
    if copyoffload_cfg.get("esxi_clone_method") != "ssh":
        LOGGER.info("SSH clone method not configured, skipping SSH key setup.")
        yield
        return

    LOGGER.info("Setting up SSH key for copy-offload.")

    # Get public key
    public_key = source_provider.get_ssh_public_key()

    # Get datastore name
    datastore_id = copyoffload_cfg.get("datastore_id")
    if not datastore_id:
        pytest.fail("datastore_id is required in copyoffload config for SSH method.")
    datastore_name = source_provider.get_datastore_name_by_id(datastore_id)

    # Get ESXi credentials from the 'copyoffload' config section
    # These support environment variable overrides (COPYOFFLOAD_ESXI_HOST, etc.)
    esxi_host = get_copyoffload_credential("esxi_host", copyoffload_cfg)
    esxi_user = get_copyoffload_credential("esxi_user", copyoffload_cfg)
    esxi_password = get_copyoffload_credential("esxi_password", copyoffload_cfg)

    if not all([esxi_host, esxi_user, esxi_password]):
        pytest.fail(
            "esxi_host, esxi_user, and esxi_password are required in the 'copyoffload' section of provider config for SSH method."
        )

    # Install the key
    install_ssh_key_on_esxi(
        host=esxi_host,
        username=esxi_user,
        password=esxi_password,
        public_key=public_key,
        datastore_name=datastore_name,
    )

    yield

    # Teardown: Remove the key
    LOGGER.info("Tearing down SSH key for copy-offload.")
    remove_ssh_key_from_esxi(
        host=esxi_host,
        username=esxi_user,
        password=esxi_password,
        public_key=public_key,
    )


@pytest.fixture(scope="class")
def vmware_cloud_init_complete(
    prepared_plan: dict[str, Any],
    source_provider: VMWareProvider,
    source_provider_data: dict[str, Any],
) -> None:
    """Ensure cloud-init has finished on all VMs before migration tests run."""
    wait_for_vmware_cloud_init_all_vms(
        prepared_plan=prepared_plan,
        source_provider=source_provider,
        source_provider_data=source_provider_data,
    )


@pytest.fixture(scope="class")
def vmware_cloud_init_complete_both_plans(
    prepared_plan_1: dict[str, Any],
    prepared_plan_2: dict[str, Any],
    source_provider: VMWareProvider,
    source_provider_data: dict[str, Any],
) -> None:
    """Ensure cloud-init has finished on all VMs from both plans before migration tests run."""
    for plan in (prepared_plan_1, prepared_plan_2):
        wait_for_vmware_cloud_init_all_vms(
            prepared_plan=plan,
            source_provider=source_provider,
            source_provider_data=source_provider_data,
        )
