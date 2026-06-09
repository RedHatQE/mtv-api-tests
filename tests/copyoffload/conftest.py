from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import filelock
import pytest
from ocp_resources.forklift_controller import ForkliftController
from ocp_resources.deployment import Deployment
from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider
from libs.providers.vmware import VMWareProvider
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from utilities.copyoffload_constants import (
    POPULATOR_INFLIGHT_LIMIT,
    SUPPORTED_VENDORS,
)
from utilities.copyoffload_migration import (
    get_copyoffload_credential,
    merge_storage_secret_extra,
    wait_for_vmware_cloud_init_all_vms,
)
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
    """Validate copy-offload configuration before running copy-offload tests.

    This fixture performs all necessary validations:
    - Verifies vSphere provider type
    - Checks for copyoffload configuration
    - Validates storage credentials availability

    Args:
        source_provider (BaseProvider): The source provider to validate.
        source_provider_data (dict[str, Any]): Source provider configuration data.
        request (pytest.FixtureRequest): Pytest request object to access CLI options.

    Returns:
        None

    Raises:
        ValueError: If provider type is not vSphere, copyoffload config is missing,
            credentials are missing, or required parameters are missing.
    """
    providers_path = resolve_providers_json_path(cli_path=request.config.getoption("providers_json"))

    # Validate that this is a vSphere provider
    if source_provider.type != Provider.ProviderType.VSPHERE:
        raise ValueError(
            f"Copy-offload tests require vSphere provider, but got '{source_provider.type}'. "
            f"Check your provider configuration in {providers_path}"
        )

    # Validate copy-offload configuration exists
    if "copyoffload" not in source_provider_data:
        raise ValueError(
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
        raise ValueError(
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


@pytest.fixture(scope="class")
def multi_datastore_config(source_provider_data: dict[str, Any]) -> None:
    """Validate multi-datastore configuration for copy-offload tests using a secondary datastore.

    Args:
        source_provider_data (dict[str, Any]): Source provider configuration data.

    Returns:
        None

    Raises:
        ValueError: If secondary_datastore_id is missing.
    """
    copyoffload_config_data: dict[str, Any] = source_provider_data.get("copyoffload", {})
    secondary_datastore_id: str | None = copyoffload_config_data.get("secondary_datastore_id")

    if not secondary_datastore_id:
        raise ValueError(
            "Multi-datastore copy-offload tests require 'secondary_datastore_id' "
            "to be configured in the copyoffload section."
        )

    LOGGER.info("✓ Multi-datastore configuration validated: secondary_datastore_id = %s", secondary_datastore_id)


def _ensure_secure_shared_lock_dir(lock_dir: Path) -> None:
    """Validate permissions on a cross-worker shared lock directory.

    Args:
        lock_dir (Path): Directory used for pytest-xdist file locks.

    Raises:
        PermissionError: If the directory is a symlink or owned by another user.
    """
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    if lock_dir.is_symlink():
        raise PermissionError(
            f"Security error: shared directory {lock_dir} is a symlink. This may indicate a hijack attempt."
        )

    current_uid = os.getuid()
    dir_stat = lock_dir.lstat()
    if dir_stat.st_uid != current_uid:
        raise PermissionError(
            f"Security error: shared directory {lock_dir} is owned by uid {dir_stat.st_uid}, "
            f"expected current user uid {current_uid}. This may indicate a hijack attempt."
        )
    os.chmod(lock_dir, 0o700)


def _forkliftcontroller_populator_inflight_lock_path() -> Path:
    """Return the cross-worker lock path for ForkliftController populator limit changes."""
    lock_dir = Path(tempfile.gettempdir()) / "pytest-shared-forklift"
    _ensure_secure_shared_lock_dir(lock_dir=lock_dir)
    return lock_dir / "populator-inflight.lock"


_POPULATOR_CONTROLLER_DEPLOYMENT = "forklift-volume-populator-controller"
_MAX_POPULATOR_INFLIGHT_ENV = "MAX_POPULATOR_INFLIGHT"


def _get_populator_inflight_from_deployment(deployment: Deployment) -> str | None:
    """Read MAX_POPULATOR_INFLIGHT from the populator controller deployment.

    Args:
        deployment (Deployment): Populator controller deployment resource.

    Returns:
        str | None: Configured in-flight limit, or None if the env var is absent.
    """
    containers = deployment.instance.spec.template.spec.containers
    for container in containers:
        for env_var in container.env or []:
            if env_var.name == _MAX_POPULATOR_INFLIGHT_ENV:
                return env_var.value
    return None


def _wait_for_populator_inflight_deployment(
    ocp_admin_client: "DynamicClient",
    mtv_namespace: str,
    expected_limit: int,
) -> None:
    """Wait until the populator controller deployment applies the in-flight limit.

    ForkliftController spec changes propagate to the populator-controller Deployment
    asynchronously. Migration must not start until MAX_POPULATOR_INFLIGHT is active.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where the populator controller runs.
        expected_limit (int): Expected MAX_POPULATOR_INFLIGHT value.

    Raises:
        TimeoutError: If the deployment does not reach the expected limit in time.
    """
    expected_value = str(expected_limit)

    def _deployment_ready_with_limit() -> bool:
        current_deployment = Deployment(
            client=ocp_admin_client,
            name=_POPULATOR_CONTROLLER_DEPLOYMENT,
            namespace=mtv_namespace,
            ensure_exists=True,
        )
        deployment_status = current_deployment.instance.status
        spec_replicas = current_deployment.instance.spec.replicas or 1
        available_replicas = deployment_status.availableReplicas or 0 if deployment_status else 0
        if available_replicas < spec_replicas:
            return False
        return _get_populator_inflight_from_deployment(deployment=current_deployment) == expected_value

    try:
        for _ in TimeoutSampler(
            wait_timeout=300,
            sleep=2,
            func=_deployment_ready_with_limit,
        ):
            if _:
                LOGGER.info(
                    f"Populator controller deployment has {_MAX_POPULATOR_INFLIGHT_ENV}={expected_value} "
                    "and is fully available"
                )
                return
    except TimeoutExpiredError as err:
        final_deployment = Deployment(
            client=ocp_admin_client,
            name=_POPULATOR_CONTROLLER_DEPLOYMENT,
            namespace=mtv_namespace,
            ensure_exists=True,
        )
        current_limit = _get_populator_inflight_from_deployment(deployment=final_deployment)
        raise TimeoutError(
            f"Timed out waiting for {_POPULATOR_CONTROLLER_DEPLOYMENT} to apply "
            f"{_MAX_POPULATOR_INFLIGHT_ENV}={expected_value} (current={current_limit!r})"
        ) from err


def _restore_forkliftcontroller_populator_inflight(
    ocp_admin_client: "DynamicClient",
    mtv_namespace: str,
    original_cr_limit: int | None,
    original_deployment_limit: int,
) -> None:
    """Restore ForkliftController populator in-flight limit to its pre-test value.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where ForkliftController is installed.
        original_cr_limit (int | None): controller_max_populator_inflight before the test.
        original_deployment_limit (int): MAX_POPULATOR_INFLIGHT on the deployment before the test.
    """
    restore_cr_limit = original_cr_limit if original_cr_limit is not None else original_deployment_limit
    forklift_controller = ForkliftController(
        client=ocp_admin_client,
        name="forklift-controller",
        namespace=mtv_namespace,
        ensure_exists=True,
    )
    current_limit = getattr(forklift_controller.instance.spec, "controller_max_populator_inflight", None)
    if current_limit == restore_cr_limit:
        LOGGER.info(
            f"ForkliftController controller_max_populator_inflight already {restore_cr_limit!r} (pre-test value)"
        )
    else:
        LOGGER.info(
            f"Restoring ForkliftController controller_max_populator_inflight "
            f"from {current_limit!r} to pre-test value {restore_cr_limit!r}"
        )
        ResourceEditor(
            patches={forklift_controller: {"spec": {"controller_max_populator_inflight": restore_cr_limit}}}
        ).update(backup_resources=False)
        forklift_controller.wait_for_condition(
            status=forklift_controller.Condition.Status.TRUE,
            condition=forklift_controller.Condition.Type.SUCCESSFUL,
            timeout=300,
        )
    _wait_for_populator_inflight_deployment(
        ocp_admin_client=ocp_admin_client,
        mtv_namespace=mtv_namespace,
        expected_limit=original_deployment_limit,
    )


@pytest.fixture(scope="class")
def populator_inflight_forkliftcontroller(
    ocp_admin_client: "DynamicClient",
    mtv_namespace: str,
) -> Generator[None, None, None]:
    """Set ForkliftController populator in-flight limit for the test class and restore pre-test value after.

    Patches controller_max_populator_inflight to POPULATOR_INFLIGHT_LIMIT (2) for the class,
    then restores the CR and deployment limits observed before setup on teardown. A file lock
    serializes ForkliftController changes across pytest-xdist workers for the entire class
    duration (setup through check_vms), including migration and post-migration verification.

    This fixture mutates cluster-wide MTV populator settings. Do not run multiple populator
    throttling test classes against the same cluster in parallel.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): Namespace where ForkliftController is installed.

    Yields:
        None
    """
    lock_path = _forkliftcontroller_populator_inflight_lock_path()
    try:
        with filelock.FileLock(lock_path, timeout=3600):
            forklift_controller = ForkliftController(
                client=ocp_admin_client,
                name="forklift-controller",
                namespace=mtv_namespace,
                ensure_exists=True,
            )
            forklift_controller.wait_for_condition(
                status=forklift_controller.Condition.Status.TRUE,
                condition=forklift_controller.Condition.Type.RUNNING,
                timeout=300,
            )

            original_cr_limit = getattr(forklift_controller.instance.spec, "controller_max_populator_inflight", None)
            initial_deployment = Deployment(
                client=ocp_admin_client,
                name=_POPULATOR_CONTROLLER_DEPLOYMENT,
                namespace=mtv_namespace,
                ensure_exists=True,
            )
            original_deployment_limit_str = _get_populator_inflight_from_deployment(deployment=initial_deployment)
            if original_deployment_limit_str is None:
                raise ValueError(
                    f"{_MAX_POPULATOR_INFLIGHT_ENV} not found on {_POPULATOR_CONTROLLER_DEPLOYMENT} "
                    f"before populator throttling test setup"
                )
            original_deployment_limit = int(original_deployment_limit_str)

            try:
                if original_cr_limit != POPULATOR_INFLIGHT_LIMIT:
                    LOGGER.info(
                        f"Setting ForkliftController controller_max_populator_inflight from {original_cr_limit!r} "
                        f"to {POPULATOR_INFLIGHT_LIMIT}"
                    )
                    ResourceEditor(
                        patches={
                            forklift_controller: {
                                "spec": {"controller_max_populator_inflight": POPULATOR_INFLIGHT_LIMIT}
                            }
                        }
                    ).update(backup_resources=False)
                    forklift_controller.wait_for_condition(
                        status=forklift_controller.Condition.Status.TRUE,
                        condition=forklift_controller.Condition.Type.SUCCESSFUL,
                        timeout=300,
                    )
                else:
                    LOGGER.info(
                        f"ForkliftController controller_max_populator_inflight already {POPULATOR_INFLIGHT_LIMIT}"
                    )

                _wait_for_populator_inflight_deployment(
                    ocp_admin_client=ocp_admin_client,
                    mtv_namespace=mtv_namespace,
                    expected_limit=POPULATOR_INFLIGHT_LIMIT,
                )

                yield
            finally:
                _restore_forkliftcontroller_populator_inflight(
                    ocp_admin_client=ocp_admin_client,
                    mtv_namespace=mtv_namespace,
                    original_cr_limit=original_cr_limit,
                    original_deployment_limit=original_deployment_limit,
                )
    except filelock.Timeout as err:
        raise TimeoutError(
            f"Timeout (3600s) waiting for ForkliftController populator-inflight lock at {lock_path}. "
            "Another worker may be running the populator throttling test."
        ) from err


@pytest.fixture(scope="class")
def rdm_config(source_provider_data: dict[str, Any]) -> None:
    """Validate RDM configuration for copy-offload RDM disk tests.

    Args:
        source_provider_data (dict[str, Any]): Source provider configuration data.

    Returns:
        None

    Raises:
        ValueError: If rdm_lun_uuid is missing.
    """
    copyoffload_config_data: dict[str, Any] = source_provider_data.get("copyoffload", {})
    rdm_lun_uuid: str | None = copyoffload_config_data.get("rdm_lun_uuid")

    if not rdm_lun_uuid:
        raise ValueError("RDM copy-offload tests require 'rdm_lun_uuid' to be configured in the copyoffload section.")

    LOGGER.info("✓ RDM configuration validated: rdm_lun_uuid = %s", rdm_lun_uuid)


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

    assert storage_hostname is not None
    assert storage_username is not None
    assert storage_password is not None

    # Validate storage vendor product
    storage_vendor = copyoffload_cfg.get("storage_vendor_product")
    if not storage_vendor:
        raise ValueError(
            f"storage_vendor_product is required in copyoffload configuration. "
            f"Valid values: {', '.join(SUPPORTED_VENDORS)}"
        )
    if storage_vendor not in SUPPORTED_VENDORS:
        raise ValueError(
            f"Unsupported storage_vendor_product '{storage_vendor}'. Valid values: {', '.join(SUPPORTED_VENDORS)}"
        )

    # Base secret data (required for all vendors)
    secret_data: dict[str, str] = {
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
    missing_vendors = set(SUPPORTED_VENDORS) - set(vendor_specific_fields)
    extra_vendors = set(vendor_specific_fields) - set(SUPPORTED_VENDORS)
    if missing_vendors or extra_vendors:
        raise ValueError(
            "vendor_specific_fields keys must match SUPPORTED_VENDORS. "
            f"Missing: {missing_vendors}. Extra: {extra_vendors}"
        )

    # Add vendor-specific fields if configured
    if storage_vendor in vendor_specific_fields:
        for config_key, secret_key, required in vendor_specific_fields[storage_vendor]:
            value = get_copyoffload_credential(config_key, copyoffload_cfg)
            if value:
                secret_data[secret_key] = value
                LOGGER.info(f"✓ Added vendor-specific field: {secret_key}")
            elif required:
                env_var_name = f"COPYOFFLOAD_{config_key.upper()}"
                raise ValueError(
                    f"Required vendor-specific field '{config_key}' not found for vendor '{storage_vendor}'. "
                    f"Add it to {providers_path} copyoffload section or set environment variable: {env_var_name}"
                )

    secret_data = merge_storage_secret_extra(secret_data, copyoffload_cfg)

    LOGGER.info(f"Creating storage secret for copy-offload with vendor: {storage_vendor}")

    storage_secret = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        namespace=target_namespace,
        string_data=secret_data,
    )

    LOGGER.info(f"✓ Copy-offload storage secret created: {storage_secret.name}")
    return storage_secret


@pytest.fixture(scope="session")
def copyoffload_ssh_key(
    source_provider: VMWareProvider,
    source_provider_data: dict[str, Any],
    copyoffload_config: None,
) -> Generator[None, None, None]:
    """SSH key on ESXi host for copy-offload if SSH method is enabled.

    Depends on copyoffload_config to ensure validation runs first.

    Args:
        source_provider (VMWareProvider): The VMware source provider instance.
        source_provider_data (dict[str, Any]): Source provider configuration data.
        copyoffload_config (None): Copy-offload configuration (validates prerequisites).

    Yields:
        None

    Raises:
        ValueError: If datastore_id or ESXi credentials are missing.
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
        raise ValueError("datastore_id is required in copyoffload config for SSH method.")
    datastore_name = source_provider.get_datastore_name_by_id(datastore_id)

    # Get ESXi credentials from the 'copyoffload' config section
    # These support environment variable overrides (COPYOFFLOAD_ESXI_HOST, etc.)
    esxi_host = get_copyoffload_credential("esxi_host", copyoffload_cfg)
    esxi_user = get_copyoffload_credential("esxi_user", copyoffload_cfg)
    esxi_password = get_copyoffload_credential("esxi_password", copyoffload_cfg)

    if not esxi_host or not esxi_user or not esxi_password:
        raise ValueError(
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
def vmware_cloud_init_ready(
    prepared_plan: dict[str, Any],
    source_provider: VMWareProvider,
    source_provider_data: dict[str, Any],
) -> None:
    """Ensure cloud-init has finished on all VMs before migration tests run.

    Args:
        prepared_plan (dict[str, Any]): Processed test plan configuration.
        source_provider (VMWareProvider): The VMware source provider instance.
        source_provider_data (dict[str, Any]): Source provider configuration data.

    Returns:
        None
    """
    wait_for_vmware_cloud_init_all_vms(
        prepared_plan=prepared_plan,
        source_provider=source_provider,
        source_provider_data=source_provider_data,
    )


@pytest.fixture(scope="class")
def vmware_cloud_init_ready_both_plans(
    prepared_plan_1: dict[str, Any],
    prepared_plan_2: dict[str, Any],
    source_provider: VMWareProvider,
    source_provider_data: dict[str, Any],
) -> None:
    """Ensure cloud-init has finished on all VMs from both plans before migration tests run.

    Args:
        prepared_plan_1 (dict[str, Any]): First processed test plan configuration.
        prepared_plan_2 (dict[str, Any]): Second processed test plan configuration.
        source_provider (VMWareProvider): The VMware source provider instance.
        source_provider_data (dict[str, Any]): Source provider configuration data.

    Returns:
        None
    """
    for plan in (prepared_plan_1, prepared_plan_2):
        wait_for_vmware_cloud_init_all_vms(
            prepared_plan=plan,
            source_provider=source_provider,
            source_provider_data=source_provider_data,
        )


@pytest.fixture(scope="class")
def nonpersistent_disk_ready(
    vmware_cloud_init_ready: None,
    prepared_plan: dict[str, Any],
    source_provider: VMWareProvider,
) -> None:
    """Change added disk mode to independent_nonpersistent after cloud-init completes.

    independent_nonpersistent disks lose data on power-off, so the disk must be
    created as regular persistent during clone (for cloud-init), then changed
    to independent_nonpersistent after the VM is powered off.

    Args:
        vmware_cloud_init_ready (None): Ensures cloud-init has finished (VM may still be on).
        prepared_plan (dict[str, Any]): Processed test plan with VM data.
        source_provider (VMWareProvider): The VMware source provider instance.
    """
    for vm_data in prepared_plan["virtual_machines"]:
        vm_name = vm_data["name"]
        provider_vm_api = prepared_plan["source_vms_data"][vm_name]["provider_vm_api"]
        if provider_vm_api.runtime.powerState == provider_vm_api.runtime.powerState.poweredOn:
            source_provider.shutdown_vm_guest(vm=provider_vm_api)
        source_provider.change_disk_mode(
            vm=provider_vm_api,
            disk_mode="independent_nonpersistent",
        )
