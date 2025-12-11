"""
Copy-offload migration tests for MTV.

This module implements tests for copy-offload functionality using the
vsphere-xcopy-volume-populator to migrate VMs with shared storage between
vSphere and OpenShift environments.
"""

import pytest
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger
import time

from urllib.parse import urlparse

from utilities.mtv_migration import (
    get_network_migration_map,
    get_storage_migration_map,
    migrate_vms,
    verify_vm_disk_count,
)


LOGGER = get_logger(__name__)


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
    - Proper datastore_ids configuration matching the VM's datastores

    Configuration in .providers.json:
    "copyoffload": {
        "storage_vendor_product": "ontap",  # or "vantara"
        "datastore_ids": ["datastore-123", "datastore-456"],    # List of vSphere datastore IDs that support copyoffload (first is default)
        "template_name": "<copyoffload-template-name>",
        "storage_hostname": "storage.example.com",
        "storage_username": "admin",
        "storage_password": "password",  # pragma: allowlist secret
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
    datastore_ids = copyoffload_config_data.get("datastore_ids", [])
    storage_class = py_config["storage_class"]

    # Use only the first (default) datastore for single-datastore tests
    datastore_id = datastore_ids[0] if datastore_ids else None

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
        datastore_ids=[datastore_id],
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
    - Proper datastore_ids configuration matching the VM's datastores
    - VM must be on a datastore that supports xcopyoff functionality

    Configuration in .providers.json:
    "copyoffload": {
        "storage_vendor_product": "ontap",  # or "vantara"
        "datastore_ids": ["datastore-123", "datastore-456"],    # List of vSphere datastore IDs that support copyoffload (first is default)
        "template_name": "<copyoffload-template-name>",
        "storage_hostname": "storage.example.com",
        "storage_username": "admin",
        "storage_password": "password",  # pragma: allowlist secret
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
    datastore_ids = copyoffload_config_data.get("datastore_ids", [])
    storage_class = py_config["storage_class"]

    # Use only the first (default) datastore for single-datastore tests
    datastore_id = datastore_ids[0] if datastore_ids else None

    # Validate required copy-offload parameters
    if not all([storage_vendor_product, datastore_id]):
        pytest.fail(
            "Missing required copy-offload parameters in config: "
            "'storage_vendor_product' and 'datastore_ids' must be set."
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
        datastore_ids=[datastore_id],
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
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_disk_migration"])],
    indirect=True,
    ids=["copyoffload-multi-disk"],
)
def test_copyoffload_multi_disk_migration(
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
    Test copy-offload migration of a VM with multiple disks.

    This test validates that a VM with multiple disks (an OS disk plus one or more
    data disks) can be successfully migrated using storage array XCOPY capabilities.
    It ensures that all disks associated with the VM are correctly handled during
    the accelerated migration process.

    Test Workflow:
    1.  Clones a VM from a template and dynamically adds one or more data disks
        as defined in the test configuration (via the 'plan' fixture).
    2.  Validates the copy-offload configuration (via copyoffload_config fixture).
    3.  Creates a storage secret for storage array authentication (via copyoffload_storage_secret fixture).
    4.  Creates network and storage migration maps with the appropriate copy-offload parameters.
    5.  Executes the migration using copy-offload.
    6.  Verifies that the migrated VM in OpenShift has the correct total number of disks.

    Requirements:
    -   vSphere provider with VMs on XCOPY-capable storage (e.g., NetApp iSCSI).
    -   Shared storage between vSphere and OpenShift (NetApp ONTAP, Hitachi Vantara).
    -   Storage class in OpenShift that supports the same storage type as the source.
    -   Storage credentials via environment variables or .providers.json config.
    -   ForkliftController with feature_copy_offload: "true" (must be pre-configured).
    -   Proper datastore_ids configuration matching the VM's datastores.

    Configuration in .providers.json:
    "copyoffload": {
        "storage_vendor_product": "ontap",  # or "vantara"
        "datastore_ids": ["datastore-123", "datastore-456"],    # List of vSphere datastore IDs that support copyoffload (first is default)
        "template_name": "<copyoffload-template-name>",
        "storage_hostname": "storage.example.com",
        "storage_username": "admin",
        "storage_password": "password",  # pragma: allowlist secret
        "ontap_svm": "vserver-name"  # For NetApp ONTAP only
    }

    Optional Environment Variables (override .providers.json values):
    -   COPYOFFLOAD_STORAGE_HOSTNAME
    -   COPYOFFLOAD_STORAGE_USERNAME
    -   COPYOFFLOAD_STORAGE_PASSWORD
    -   COPYOFFLOAD_ONTAP_SVM

    Args:
        plan: Migration plan configuration from test parameters.
        source_provider: Source provider (vSphere).
        source_provider_inventory: Source provider inventory.
        target_namespace: Target namespace for migration.
        ocp_admin_client: OpenShift admin client.
        copyoffload_config: Copy-offload configuration validation fixture.
        copyoffload_storage_secret: Storage secret for copy-offload authentication.
        multus_network_name: Multus network configuration name.
        source_vms_network: Source VMs network configuration.
        source_vms_namespace: Source VMs namespace.
        warm_migration: Boolean flag for warm migration.
        destination_provider: Destination provider (OpenShift).
        request: Pytest request object.
        fixture_store: Pytest fixture store for resource tracking.
    """
    # The 'plan' fixture handles cloning the VM with the additional disk.
    # This test function will execute after the VM is cloned.

    # Get copy-offload configuration
    copyoffload_config_data = source_provider_data["copyoffload"]
    storage_vendor_product = copyoffload_config_data.get("storage_vendor_product")
    datastore_ids = copyoffload_config_data.get("datastore_ids", [])
    storage_class = py_config["storage_class"]

    # Use only the first (default) datastore for single-datastore tests
    datastore_id = datastore_ids[0] if datastore_ids else None

    # Create network migration map
    vms = [vm["name"] for vm in plan["virtual_machines"]]
    network_migration_map = get_network_migration_map(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        vms=vms,
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
        vms=vms,
        storage_class=storage_class,
        # Copy-offload specific parameters
        datastore_ids=[datastore_id],
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

    # Verify that the correct number of disks were migrated
    verify_vm_disk_count(destination_provider=destination_provider, plan=plan, target_namespace=target_namespace)


@pytest.mark.copyoffload
@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_datastore_migration"])],
    indirect=True,
    ids=["copyoffload-multi-datastore"],
)
def test_copyoffload_multi_datastore_migration(
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
    Test copy-offload migration of a VM with disks on multiple datastores using
    the same storage system.

    This test validates copy-offload functionality when a VM has:
    - One disk on the primary/default datastore (from the template)
    - One additional disk on a secondary datastore on the same storage system.

    This ensures that copy-offload can handle VMs with disks distributed across
    multiple datastores.

    Test Workflow:
    1. Validates copy-offload configuration (via copyoffload_config fixture)
    2. Creates storage secret for storage array authentication (via copyoffload_storage_secret fixture)
    3. Clones VM from template with an additional disk on the secondary datastore
    4. Creates network migration map
    5. Builds copy-offload plugin configuration
    6. Creates storage map with multiple datastores (primary and secondary)
    7. Executes migration using copy-offload technology
    8. Verifies successful migration and VM operation in OpenShift
    9. Verifies that all disks were migrated correctly

    Requirements:
    -   vSphere provider with VMs on XCOPY-capable storage (e.g., NetApp iSCSI).
    -   Shared storage between vSphere and OpenShift (NetApp ONTAP, Hitachi Vantara).
    -   Storage class in OpenShift that supports the same storage type as the source.
    -   Storage credentials via environment variables or .providers.json config.
    -   ForkliftController with feature_copy_offload: "true" (must be pre-configured).
    -   Two datastores configured using the same storage system.
    -   Proper datastore_ids configuration matching the VM's datastores.

    Configuration in .providers.json:
    "copyoffload": {
        "storage_vendor_product": "ontap",  # or "vantara"
        "datastore_ids": ["datastore-123", "datastore-456"],    # List of vSphere datastore IDs that support copyoffload (first is default/primary)
        "template_name": "<copyoffload-template-name>",
        "storage_hostname": "storage.example.com",
        "storage_username": "admin",
        "storage_password": "password",  # pragma: allowlist secret
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
    datastore_ids = copyoffload_config_data.get("datastore_ids", [])
    storage_class = py_config["storage_class"]

    # Use the first datastore as default (for the template disk)
    default_datastore_id = datastore_ids[0] if datastore_ids else None

    # Validate required copy-offload parameters
    if not all([storage_vendor_product, default_datastore_id]):
        pytest.fail(
            "Missing required copy-offload parameters in config: "
            "'storage_vendor_product' and 'datastore_ids' must be set."
        )

    # For multi-datastore test, ensure we have at least 2 datastores configured
    if len(datastore_ids) < 2:
        pytest.fail(
            f"Multi-datastore test requires at least 2 datastores in 'datastore_ids' list. Found {len(datastore_ids)}."
        )

    # Collect all datastore IDs from the VM configuration
    # Resolve any markers (like "SECONDARY_DATASTORE") to actual datastore IDs
    datastores_to_use = [default_datastore_id]  # Start with default datastore

    vm_config = plan["virtual_machines"][0] if plan["virtual_machines"] else {}
    add_disks = vm_config.get("add_disks", [])

    # Gather unique datastore IDs from the disk configurations, resolving markers
    for disk in add_disks:
        disk_datastore_id = disk.get("datastore_id")

        # Resolve SECONDARY_DATASTORE marker
        if disk_datastore_id == "SECONDARY_DATASTORE":
            if len(datastore_ids) >= 2:
                disk_datastore_id = datastore_ids[1]
                LOGGER.info(f"Resolved SECONDARY_DATASTORE marker to: {disk_datastore_id}")
            else:
                LOGGER.warning(
                    f"SECONDARY_DATASTORE marker found but only {len(datastore_ids)} datastore(s) configured"
                )
                disk_datastore_id = None

        if disk_datastore_id and disk_datastore_id not in datastores_to_use:
            datastores_to_use.append(disk_datastore_id)
            LOGGER.info(f"Additional disk configured with datastore ID: {disk_datastore_id}")

    LOGGER.info(f"Multi-datastore migration will use {len(datastores_to_use)} datastore(s): {datastores_to_use}")

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

    # Create storage migration map with datastores needed for this VM
    # If VM has only one disk (no add_disks or all disks on primary), use only primary datastore
    # If VM has disks on multiple datastores, include all relevant datastores
    storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        target_namespace=target_namespace,
        source_provider=source_provider,
        destination_provider=destination_provider,
        ocp_admin_client=ocp_admin_client,
        source_provider_inventory=source_provider_inventory,
        vms=vms_names,
        storage_class=storage_class,
        datastore_ids=datastores_to_use,
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

    # Verify that the correct number of disks were migrated (1 base + 1 added = 2 total)
    verify_vm_disk_count(destination_provider=destination_provider, plan=plan, target_namespace=target_namespace)
