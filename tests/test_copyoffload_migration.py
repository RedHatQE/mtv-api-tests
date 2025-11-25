"""
Copy-offload migration tests for MTV.

This module implements tests for copy-offload functionality using the
vsphere-xcopy-volume-populator to migrate VMs with shared storage between
vSphere and OpenShift environments.
"""

import pytest
from pytest_testconfig import config as py_config

from utilities.mtv_migration import get_network_migration_map, get_storage_migration_map, migrate_vms


@pytest.mark.copyoffload
@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thin_migration"])],
    indirect=True,
    ids=["copyoffload-thin"],
)
def test_copyoffload_thin_migration(
    request,
    fixture_store,
    ocp_admin_client,
    target_namespace,
    destination_provider,
    plan,
    source_provider,
    source_provider_data,
    multus_network_name,
    source_provider_inventory,
    source_vms_namespace,
    copyoffload_config,
    copyoffload_storage_secret,
):
    """
    Test copy-offload migration of a thin-provisioned VM disk.

    This test validates copy-offload functionality using storage array XCOPY
    capabilities to accelerate VM disk migrations from VMware vSphere to OpenShift,
    reducing migration time from hours to minutes.

    Test Workflow:
    1. Validates copy-offload configuration (via copyoffload_config fixture)
    2. Creates storage secret for storage array authentication (via copyoffload_storage_secret fixture)
    3. Creates network migration map
    4. Builds copy-offload plugin configuration
    5. Creates storage map with copy-offload parameters
    6. Executes migration using copy-offload technology
    7. Verifies successful migration and VM operation in OpenShift

    Requirements:
    - vSphere provider with VMs on XCOPY-capable storage
    - Shared storage between vSphere and OpenShift (NetApp ONTAP, Hitachi Vantara)
    - Storage credentials via environment variables or .providers.json config
    - ForkliftController with feature_copy_offload: "true" (must be pre-configured)
    - Proper datastore_id configuration matching the VM's datastore

    Configuration in .providers.json:
    "copyoffload": {
        "storage_vendor_product": "ontap",  # or "vantara"
        "datastore_id": "datastore-123",    # vSphere datastore ID
        "template_name": "<copyoffload-template-name>",
        "storage_hostname": "storage.example.com",
        "storage_username": "admin",
        "storage_password": "password",
        "ontap_svm": "vserver-name"  # For NetApp ONTAP only
    }

    Optional Environment Variables (override .providers.json values):
    - COPYOFFLOAD_STORAGE_HOSTNAME
    - COPYOFFLOAD_STORAGE_USERNAME
    - COPYOFFLOAD_STORAGE_PASSWORD
    - COPYOFFLOAD_ONTAP_SVM

    Args:
        request: Pytest request object
        fixture_store: Pytest fixture store for resource tracking
        ocp_admin_client: OpenShift admin client
        target_namespace: Target namespace for migration
        destination_provider: Destination provider (OpenShift)
        plan: Migration plan configuration from test parameters
        source_provider: Source provider (vSphere)
        source_provider_data: Source provider configuration data
        multus_network_name: Multus network configuration name
        source_provider_inventory: Source provider inventory
        source_vms_namespace: Source VMs namespace
        copyoffload_config: Copy-offload configuration validation fixture
        copyoffload_storage_secret: Storage secret for copy-offload authentication
    """

    # Get copy-offload configuration
    copyoffload_config_data = source_provider_data["copyoffload"]
    storage_vendor_product = copyoffload_config_data.get("storage_vendor_product")
    datastore_id = copyoffload_config_data.get("datastore_id")
    storage_class = py_config["storage_class"]

    # Create network migration map
    vms_names = [vm["name"] for vm in plan["virtual_machines"]]
    network_migration_map = get_network_migration_map(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        vms=vms_names,
    )

    # Build offload plugin configuration
    offload_plugin_config = {
        "vsphereXcopyConfig": {
            "secretRef": copyoffload_storage_secret.name,
            "storageVendorProduct": storage_vendor_product,
        }
    }

    # Create storage migration map with copy-offload configuration
    storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        target_namespace=target_namespace,
        source_provider=source_provider,
        destination_provider=destination_provider,
        ocp_admin_client=ocp_admin_client,
        source_provider_inventory=source_provider_inventory,
        vms=vms_names,
        storage_class=storage_class,
        # Copy-offload specific parameters
        datastore_id=datastore_id,
        offload_plugin_config=offload_plugin_config,
        access_mode="ReadWriteOnce",
        volume_mode="Block",
    )

    # Execute copy-offload migration
    migrate_vms(
        ocp_admin_client=ocp_admin_client,
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
        source_provider_inventory=source_provider_inventory,
    )


@pytest.mark.copyoffload
@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thick_lazy_migration"])],
    indirect=True,
    ids=["copyoffload-thick-lazy"],
)
def test_copyoffload_thick_lazy_migration(
    request,
    fixture_store,
    ocp_admin_client,
    target_namespace,
    destination_provider,
    plan,
    source_provider,
    source_provider_data,
    multus_network_name,
    source_provider_inventory,
    source_vms_namespace,
    copyoffload_config,
    copyoffload_storage_secret,
):
    """
    Test copy-offload migration of a thick (lazy) disk VM.

    This test validates copy-offload functionality using storage array XCOPY
    capabilities to accelerate VM disk migrations from VMware vSphere to OpenShift
    for thick (lazy) provisioned disks, reducing migration time from hours to minutes.

    Test Workflow:
    1. Validates copy-offload configuration (via copyoffload_config fixture)
    2. Creates storage secret for storage array authentication (via copyoffload_storage_secret fixture)
    3. Creates network migration map
    4. Builds copy-offload plugin configuration
    5. Creates storage map with copy-offload parameters
    6. Executes migration using copy-offload technology (confirms xcopy was used)
    7. Verifies successful migration and VM operation in OpenShift
    8. Confirms VM is alive after migration

    Requirements:
    - vSphere provider with VMs on XCOPY-capable storage (e.g., NetApp iSCSI)
    - Shared storage between vSphere and OpenShift (NetApp ONTAP, Hitachi Vantara)
    - Storage class in OpenShift that supports the same storage type as source
    - Storage credentials via environment variables or .providers.json config
    - ForkliftController with feature_copy_offload: "true" (must be pre-configured)
    - Proper datastore_id configuration matching the VM's datastore
    - VM must be on a datastore that supports xcopyoff functionality

    Configuration in .providers.json:
    "copyoffload": {
        "storage_vendor_product": "ontap",  # or "vantara"
        "datastore_id": "datastore-123",    # vSphere datastore ID (must support xcopyoff)
        "template_name": "<copyoffload-template-name>",
        "storage_hostname": "storage.example.com",
        "storage_username": "admin",
        "storage_password": "password",
        "ontap_svm": "vserver-name"  # For NetApp ONTAP only
    }

    Optional Environment Variables (override .providers.json values):
    - COPYOFFLOAD_STORAGE_HOSTNAME
    - COPYOFFLOAD_STORAGE_USERNAME
    - COPYOFFLOAD_STORAGE_PASSWORD
    - COPYOFFLOAD_ONTAP_SVM

    Args:
        request: Pytest request object
        fixture_store: Pytest fixture store for resource tracking
        ocp_admin_client: OpenShift admin client
        target_namespace: Target namespace for migration
        destination_provider: Destination provider (OpenShift)
        plan: Migration plan configuration from test parameters
        source_provider: Source provider (vSphere)
        source_provider_data: Source provider configuration data
        multus_network_name: Multus network configuration name
        source_provider_inventory: Source provider inventory
        source_vms_namespace: Source VMs namespace
        copyoffload_config: Copy-offload configuration validation fixture
        copyoffload_storage_secret: Storage secret for copy-offload authentication
    """

    # Get copy-offload configuration
    copyoffload_config_data = source_provider_data["copyoffload"]
    storage_vendor_product = copyoffload_config_data.get("storage_vendor_product")
    datastore_id = copyoffload_config_data.get("datastore_id")
    storage_class = py_config["storage_class"]

    # Validate required copy-offload parameters
    if not all([storage_vendor_product, datastore_id]):
        pytest.fail(
            "Missing required copy-offload parameters in config: "
            "'storage_vendor_product' and 'datastore_id' must be set."
        )

    # Create network migration map
    vms_names = [vm["name"] for vm in plan["virtual_machines"]]
    network_migration_map = get_network_migration_map(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        vms=vms_names,
    )

    # Build offload plugin configuration
    offload_plugin_config = {
        "vsphereXcopyConfig": {
            "secretRef": copyoffload_storage_secret.name,
            "storageVendorProduct": storage_vendor_product,
        }
    }

    # Create storage migration map with copy-offload configuration
    storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        target_namespace=target_namespace,
        source_provider=source_provider,
        destination_provider=destination_provider,
        ocp_admin_client=ocp_admin_client,
        source_provider_inventory=source_provider_inventory,
        vms=vms_names,
        storage_class=storage_class,
        # Copy-offload specific parameters
        datastore_id=datastore_id,
        offload_plugin_config=offload_plugin_config,
        access_mode="ReadWriteOnce",
        volume_mode="Block",
    )

    # Execute copy-offload migration
    migrate_vms(
        ocp_admin_client=ocp_admin_client,
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
        source_provider_inventory=source_provider_inventory,
    )
