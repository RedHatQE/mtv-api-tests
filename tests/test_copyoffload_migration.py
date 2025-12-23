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

from ocp_resources.virtual_machine import VirtualMachine
from utilities.mtv_migration import (
    get_network_migration_map,
    get_storage_migration_map,
    migrate_vms,
    verify_vm_disk_count,
)
from utilities.utils import (
    get_guest_credential,
    run_ssh_command,
)
from utilities.ssh_utils import VMSSHConnection

LOGGER = get_logger(__name__)


@pytest.mark.copyoffload
@pytest.mark.parametrize(
    "plan,multus_network_name",
    [
        pytest.param(
            py_config["tests_params"]["test_copyoffload_thin_migration"],
            py_config["tests_params"]["test_copyoffload_thin_migration"],
        )
    ],
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
    setup_copyoffload_ssh,
    vm_ssh_connections,
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
    storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
    datastore_id = copyoffload_config_data["datastore_id"]
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
    "plan,multus_network_name",
    [
        pytest.param(
            py_config["tests_params"]["test_copyoffload_thick_lazy_migration"],
            py_config["tests_params"]["test_copyoffload_thick_lazy_migration"],
        )
    ],
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
    setup_copyoffload_ssh,
    vm_ssh_connections,
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
    storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
    datastore_id = copyoffload_config_data["datastore_id"]
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
        vm_ssh_connections=vm_ssh_connections,
    )


@pytest.mark.copyoffload
@pytest.mark.parametrize(
    "plan,multus_network_name",
    [
        pytest.param(
            py_config["tests_params"]["test_copyoffload_multi_disk_migration"],
            py_config["tests_params"]["test_copyoffload_multi_disk_migration"],
        )
    ],
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
    setup_copyoffload_ssh,
    vm_ssh_connections,
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
    -   Proper datastore_id configuration matching the VM's datastore.

    Configuration in .providers.json:
    "copyoffload": {
        "storage_vendor_product": "ontap",  # or "vantara"
        "datastore_id": "datastore-123",    # vSphere datastore ID (must support xcopyoff)
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
    storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
    datastore_id = copyoffload_config_data["datastore_id"]
    storage_class = py_config["storage_class"]

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
        vm_ssh_connections=vm_ssh_connections,
    )

    # Verify that the correct number of disks were migrated
    verify_vm_disk_count(destination_provider=destination_provider, plan=plan, target_namespace=target_namespace)


@pytest.mark.copyoffload
@pytest.mark.parametrize(
    "plan,multus_network_name",
    [
        pytest.param(
            py_config["tests_params"]["test_copyoffload_multi_disk_different_path_migration"],
            py_config["tests_params"]["test_copyoffload_multi_disk_different_path_migration"],
        )
    ],
    indirect=True,
    ids=["copyoffload-multi-disk-different-path"],
)
def test_copyoffload_multi_disk_different_path_migration(
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
    setup_copyoffload_ssh,
):
    """
    Test copy-offload migration of a multi-disk VM where an additional disk
    resides in a different folder on the same datastore.

    This test validates that a VM with multiple disks can be migrated using XCOPY,
    even when one of its disks (.vmdk files) resides in a different directory
    path on the same datastore as the primary VM folder.

    Test Workflow:
    1.  Clones a VM from a template and dynamically adds a data disk into a
        separate, specified folder on the same datastore.
    2.  Validates the copy-offload configuration.
    3.  Creates a storage secret for storage array authentication.
    4.  Creates network and storage migration maps with copy-offload parameters.
    5.  Executes the migration using copy-offload.
    6.  Verifies that the migrated VM in OpenShift has the correct total number of disks.

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
    # The 'plan' fixture handles cloning the VM with the additional disk in a different path.
    # This test function will execute after the VM is cloned.

    # Get copy-offload configuration
    copyoffload_config_data = source_provider_data["copyoffload"]
    storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
    datastore_id = copyoffload_config_data["datastore_id"]
    storage_class = py_config["storage_class"]

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

    # Verify that the correct number of disks were migrated
    verify_vm_disk_count(destination_provider=destination_provider, plan=plan, target_namespace=target_namespace)


@pytest.mark.copyoffload
@pytest.mark.parametrize(
    "plan,multus_network_name",
    [
        pytest.param(
            py_config["tests_params"]["test_copyoffload_rdm_virtual_disk_migration"],
            py_config["tests_params"]["test_copyoffload_rdm_virtual_disk_migration"],
        )
    ],
    indirect=True,
    ids=["copyoffload-rdm-virtual"],
)
def test_copyoffload_rdm_virtual_disk_migration(
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
    vm_ssh_connections,
):
    """
    Test copy-offload migration of a VM with an RDM (Raw Device Mapping) disk.

    This test validates that a VM with an RDM disk in virtual compatibility mode
    can be migrated using storage array XCOPY capabilities. The RDM disk is added
    post-clone since RDM requires VMFS datastore (not NFS).

    RDM Types (foundation for physical mode in place):
    -   virtual: RDM appears as virtual disk to guest. Supports snapshots. (tested here)
    -   physical: Direct SCSI passthrough to LUN. (future test)

    Requires in .providers.json copyoffload section:
    -   rdm_lun_uuid: LUN NAA identifier (e.g., "naa.600a098038313954492458313032306f")
    """
    copyoffload_config_data = source_provider_data["copyoffload"]
    storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
    datastore_id = copyoffload_config_data["datastore_id"]
    storage_class = py_config["storage_class"]

    # Validate RDM LUN is configured
    if "rdm_lun_uuid" not in copyoffload_config_data or not copyoffload_config_data["rdm_lun_uuid"]:
        pytest.fail("rdm_lun_uuid is required in copyoffload configuration for RDM disk tests")

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

    storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        target_namespace=target_namespace,
        source_provider=source_provider,
        destination_provider=destination_provider,
        ocp_admin_client=ocp_admin_client,
        source_provider_inventory=source_provider_inventory,
        vms=vms,
        storage_class=storage_class,
        datastore_id=datastore_id,
        offload_plugin_config=offload_plugin_config,
        access_mode="ReadWriteOnce",
        volume_mode="Block",
    )

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
        vm_ssh_connections=vm_ssh_connections,
    )

    # Verify that the correct number of disks were migrated (1 base + 1 RDM = 2)
    verify_vm_disk_count(destination_provider=destination_provider, plan=plan, target_namespace=target_namespace)


@pytest.mark.copyoffload
@pytest.mark.parametrize(
    "plan,multus_network_name",
    [
        pytest.param(
            py_config["tests_params"]["test_copyoffload_thin_snapshots_migration"],
            py_config["tests_params"]["test_copyoffload_thin_snapshots_migration"],
        )
    ],
    indirect=True,
    ids=["copyoffload-thin-snapshots"],
)
def test_copyoffload_thin_snapshots_migration(
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
    vm_ssh_connections,
):
    """
    Test copy-offload migration of a thin-provisioned VM disk with snapshots.

    This test validates that copy-offload migration works correctly for VMs
    that have snapshots. It uses storage array XCOPY capabilities to accelerate
    the migration of VM disks from VMware vSphere to OpenShift.

    Test Workflow:
    1. Creates one or more snapshots for the source VM.
    2. Creates a folder on the source VM after snapshots are taken to ensure the latest state is migrated.
    3. Validates copy-offload configuration (via copyoffload_config fixture).
    4. Creates a storage secret for storage array authentication (via copyoffload_storage_secret fixture).
    5. Creates a network migration map.
    6. Builds the copy-offload plugin configuration.
    7. Creates a storage map with copy-offload parameters.
    8. Executes the migration using copy-offload technology.
    9. Verifies successful migration and VM operation in OpenShift.
    10. Confirms the VM is alive after migration.

    Requirements:
    - vSphere provider with VMs on XCOPY-capable storage (e.g., NetApp iSCSI).
    - Shared storage between vSphere and OpenShift (NetApp ONTAP, Hitachi Vantara).
    - Storage class in OpenShift that supports the same storage type as the source.
    - Storage credentials via environment variables or .providers.json config.
    - ForkliftController with feature_copy_offload: "true" (must be pre-configured).
    - Proper datastore_id configuration matching the VM's datastore.
    - The VM must be on a datastore that supports xcopy functionality.

    Configuration in .providers.json:
    "copyoffload": {
        "storage_vendor_product": "ontap",  # or "vantara"
        "datastore_id": "datastore-123",    # vSphere datastore ID (must support xcopy)
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
    - GUEST_VM_LINUX_USER
    - GUEST_VM_LINUX_PASSWORD
    """
    # Create snapshots and a folder on the source VM
    test_folder = "/tmp/test_snapshot_folder"
    for vm_data in plan["virtual_machines"]:
        num_snapshots = vm_data.get("snapshots", 0)
        LOGGER.info(f"VM: {vm_data['name']}, Snapshots to create: {num_snapshots}")
        if num_snapshots > 0:
            vm_api_obj = source_provider.get_vm_by_name(query=vm_data["name"])

            # Power on the VM and get its IP
            source_provider.start_vm(vm=vm_api_obj)
            vm_ip = source_provider.get_vm_ip(vm=vm_api_obj)
            if not vm_ip:
                pytest.fail(f"Failed to get IP for VM '{vm_data['name']}'")

            # Create snapshots
            for i in range(num_snapshots):
                source_provider.create_snapshot(vm=vm_api_obj, snapshot_name=f"{vm_data['name']}-snapshot-{i}")

            # Get credentials and create folder via SSH
            user = get_guest_credential("guest_vm_linux_user", source_provider_data)
            password = get_guest_credential("guest_vm_linux_password", source_provider_data)

            LOGGER.info(f"Attempting to create folder '{test_folder}' on VM '{vm_data['name']}' at {vm_ip}")
            run_ssh_command(vm_ip=vm_ip, command=f"mkdir -p {test_folder}", username=user, password=password)
            source_provider.stop_vm(vm=vm_api_obj)

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
        vm_ssh_connections=vm_ssh_connections,
    )

    # Verify that the folder created on the source VM exists on the migrated VM
    for vm_data in plan["virtual_machines"]:
        vm_name = vm_data["name"]
        user = get_guest_credential("guest_vm_linux_user", source_provider_data)
        password = get_guest_credential("guest_vm_linux_password", source_provider_data)

        with vm_ssh_connections.create(vm_name=vm_name, username=user, password=password) as ssh_conn:
            rrmngmnt_host = ssh_conn.get_rrmngmnt_host()
            rc, _, err = rrmngmnt_host.run_command(f"test -d {test_folder}")
            assert rc == 0, f"Folder '{test_folder}' not found on migrated VM '{vm_name}': {err}"
