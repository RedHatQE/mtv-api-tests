"""
Copy-offload migration tests for MTV.

This module implements tests for copy-offload functionality using the
vsphere-xcopy-volume-populator to migrate VMs with shared storage between
vSphere and OpenShift environments.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.providers.vmware import VMWareProvider

from ocp_resources.migration import Migration
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine import VirtualMachine
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import MigrationPlanExecError
from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.openshift import OCPProvider
from utilities.copyoffload_constants import (
    POPULATOR_INFLIGHT_LIMIT,
    THROTTLE_HOST_LABEL,
    VM_INFLIGHT_LIMIT,
    VM_POPULATOR_INFLIGHT_LIMIT,
)
from utilities.copyoffload_migration import (
    create_log_capture_callback,
    execute_copyoffload_migration,
    execute_migration_monitoring_populator_inflight,
    execute_migration_monitoring_vm_and_populator_inflight,
    get_vm_esxi_host,
    resolve_invalid_dedicated_host_id,
    verify_dedicated_migration_host,
    verify_populate_pod_failure_reason,
    verify_populator_throttling,
    verify_vm_inflight_throttling,
    verify_xcopy_used,
    verify_xcopy_used_per_datastore,
)
from utilities.migration_utils import get_cutover_value
from utilities.copyoffload_plan_secret import wait_for_copyoffload_plan_secret
from utilities.mtv_migration import (
    create_plan_resource,
    get_network_migration_map,
    get_plan_migration_status,
    get_storage_migration_map,
    verify_vm_disk_count,
    wait_for_concurrent_migration_execution,
    wait_for_dual_migration_completion,
    wait_for_migration_complate,
)
from utilities.naming import sanitize_kubernetes_name
from utilities.post_migration import check_vms, verify_data_integrity, verify_rdm_disk_bus_types
from utilities.vmware_guest_operations import create_data_integrity_marker
from utilities.resources import create_and_store_resource
from utilities.ssh_utils import SSHConnectionManager

LOGGER = get_logger(__name__)

# Error message template for simultaneous migration validation
EARLY_COMPLETION_MSG = (
    "Plan {plan_num} reached {completed_status} before both plans were executing simultaneously. "
    "Other plan status: {other_status}"
)


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thin_migration"])],
    indirect=True,
    ids=["MTV-559:copyoffload-thin"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadThinMigration:
    """Copy-offload a VM with a thin-provisioned disk using XCOPY and verify the migrated VM.

    Purpose/Regression:
        Basic copy-offload scenario (MTV-559): a VM whose disk is
        thin-provisioned on XCOPY-capable shared storage must migrate with
        hardware-assisted copy (VAAI XCOPY) instead of the default transfer.
        The pass signal is dual: the populate pod logs must report XCOPY was
        used, and the migrated VM must validate. There is no induced
        failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. The VM disk must live on storage shared between
        vSphere and OpenShift that supports hardware-assisted copy (VAAI
        XCOPY, e.g. NetApp ONTAP or Hitachi Vantara), with storage-vendor
        credentials available for the copy-offload storage Secret. Prepare a
        destination storage class that provisions block volumes
        (volumeMode: Block) on that shared storage and a reachable destination
        network. Use a disposable Linux VM (e.g. cloned from a template) with
        a thin-provisioned disk and guest-agent access. Have permission to
        create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with a thin-provisioned disk, wait until
           it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin
           (secret reference and storage vendor product). Create a NetworkMap
           for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the disk.
        6. Validate the migrated VM: power state, connectivity, and CPU,
           memory, network, and storage equivalence with the source. After
           the checks, delete the migrated VM; the secret, maps, and Plan are
           removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for the disk, and the migrated
        VM matches the source. On failure, inspect the migration CR status
        for the VM's failed step, the populate pod logs for the xcopyUsed
        value and storage-vendor errors, and the StorageMap's offload plugin
        configuration.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )


class CopyoffloadSnapshotBase:
    """Base class for copy-offload snapshot migration tests with data integrity validation."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
    data_integrity_marker: str

    def test_create_snapshots_and_data_marker(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        source_provider: VMWareProvider,
        source_provider_data: dict[str, Any],
    ) -> None:
        """Create snapshots and write data integrity marker on the source VM.

        Args:
            prepared_plan: Processed test plan configuration.
            fixture_store: Session-scoped fixture store.
            source_provider: Source provider for VM operations.
            source_provider_data: Provider configuration data.
        """
        vm_cfg = prepared_plan["virtual_machines"][0]
        provider_vm_api = prepared_plan["source_vms_data"][vm_cfg["name"]]["provider_vm_api"]

        source_provider.start_vm(provider_vm_api)
        source_provider.wait_for_vmware_guest_info(provider_vm_api, timeout=60)

        snapshots_to_create = int(vm_cfg["snapshots"])
        snapshot_prefix = f"{vm_cfg['name']}-{fixture_store['session_uuid']}-snapshot"

        for idx in range(1, snapshots_to_create + 1):
            source_provider.create_snapshot(
                vm=provider_vm_api,
                name=f"{snapshot_prefix}-{idx}",
                description="mtv-api-tests copy-offload snapshots migration test",
                memory=False,
                quiesce=False,
                wait_timeout=60 * 10,
            )

        vm_cfg["snapshots_before_migration"] = source_provider.vm_dict(provider_vm_api=provider_vm_api)[
            "snapshots_data"
        ]
        assert len(vm_cfg["snapshots_before_migration"]) >= snapshots_to_create, (
            f"Expected at least {snapshots_to_create} snapshots, got {len(vm_cfg['snapshots_before_migration'])}"
        )

        marker_content = f"mtv-data-integrity-{fixture_store['session_uuid']}"
        create_data_integrity_marker(
            source_provider=source_provider,
            vm=provider_vm_api,
            source_provider_data=source_provider_data,
            marker_content=marker_content,
        )
        self.__class__.data_integrity_marker = marker_content

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY fallback was used (no XCOPY acceleration).

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=False,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )

    def test_check_data_integrity(
        self,
        prepared_plan: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider_data: dict[str, Any],
        source_provider: BaseProvider,
        vm_ssh_connections: SSHConnectionManager,
    ) -> None:
        """Verify data written before migration survived on the migrated VM.

        Starts the migrated VM, reads the marker file via SSH, and confirms
        the content matches what was written pre-migration.

        Args:
            prepared_plan (dict[str, Any]): Processed test plan configuration.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider_data (dict[str, Any]): Provider configuration data.
            source_provider (BaseProvider): Source provider for VM metadata.
            vm_ssh_connections (SSHConnectionManager): SSH connection manager.
        """
        vm_cfg = prepared_plan["virtual_machines"][0]
        vm_name = vm_cfg["name"]
        vm_namespace = prepared_plan["_vm_target_namespace"]

        vm = VirtualMachine(
            client=ocp_admin_client,
            name=vm_name,
            namespace=vm_namespace,
        )

        source_vm = source_provider.vm_dict(name=vm_name)

        try:
            if not vm.ready:
                LOGGER.info(f"Starting migrated VM {vm_name} for data integrity check")
                vm.start(wait=True, timeout=300)
            else:
                LOGGER.info(f"Migrated VM {vm_name} is already running, skipping start")
            verify_data_integrity(
                vm_name=vm_name,
                vm_ssh_connections=vm_ssh_connections,
                source_provider_data=source_provider_data,
                source_vm_info=source_vm,
                expected_marker_content=self.data_integrity_marker,
            )
        finally:
            LOGGER.info(f"Stopping VM {vm_name} after data integrity check")
            vm.stop(wait=True, timeout=300)


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thick_lazy_snapshots_migration"])],
    indirect=True,
    ids=["MTV-581:copyoffload-thick-lazy-snapshots"],
)
@pytest.mark.copyoffload_snapshots
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadThickLazySnapshotsMigration(CopyoffloadSnapshotBase):
    """Copy-offload a VM with a thick lazy-zeroed disk and source snapshots; verify data integrity and snapshot preservation.

    Purpose/Regression:
        Copy-offload of a VM that carries source snapshots (MTV-581): the
        snapshots must not break the migration, the current disk state —
        including data written after the snapshots — must arrive intact, and
        the source VM's snapshots must remain unchanged. Snapshots force the
        populator off the XCOPY path, so the expected transfer is the
        fallback. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift (XCOPY-capable, e.g. NetApp ONTAP or Hitachi Vantara),
        with storage-vendor credentials for the copy-offload storage Secret.
        Prepare a destination storage class that provisions block volumes
        (volumeMode: Block) and a reachable destination network. Use a
        disposable Linux VM (e.g. cloned from a template) with a thick
        lazy-zeroed disk, guest-agent access, and SSH access for the data
        integrity check. Have permission to create snapshots on the source
        VM and to create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Start the disposable VM, create two snapshots on it, record the
           snapshot list, and write a unique data marker file into the guest.
           Wait until the VM appears in Forklift inventory and create the
           copy-offload storage Secret.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class with the vsphereXcopyConfig offload plugin, and a
           NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify the transfer method: the populate pod logs report no XCOPY
           (fallback) for the disk with snapshots.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence, disk count, and that the source VM's snapshots are
           unchanged after migration.
        7. Start the migrated VM, read the marker file over SSH and confirm
           its content matches what was written before migration, then stop
           the VM. After the checks, delete the migrated VM; the secret,
           maps, Plan, and source snapshots are removed with the run.

    Expected result:
        The migration succeeds via the fallback transfer, the source
        snapshots are preserved, and the marker data written before migration
        is present and identical on the migrated VM. On failure, inspect the
        migration CR status, the populate pod logs, the source VM's snapshot
        list, and the marker file content on the migrated VM.
    """


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.copyoffload_sanity
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thick_eager_snapshots_migration"])],
    indirect=True,
    ids=["MTV-583:copyoffload-thick-eager-snapshots"],
)
@pytest.mark.copyoffload_snapshots
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadThickEagerSnapshotsMigration(CopyoffloadSnapshotBase):
    """Copy-offload a VM with a thick eager-zeroed disk and source snapshots; verify data integrity and snapshot preservation.

    Purpose/Regression:
        Same scenario as the thick lazy-zeroed snapshot variant (MTV-583),
        exercised with a thick eager-zeroed disk: source snapshots must not
        break the copy-offload migration, current disk data must arrive
        intact, and the source snapshots must remain unchanged. Snapshots
        force the fallback transfer. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift (XCOPY-capable, e.g. NetApp ONTAP or Hitachi Vantara),
        with storage-vendor credentials for the copy-offload storage Secret.
        Prepare a destination storage class that provisions block volumes
        (volumeMode: Block) and a reachable destination network. Use a
        disposable Linux VM (e.g. cloned from a template) with a thick
        eager-zeroed disk, guest-agent access, and SSH access for the data
        integrity check. Have permission to create snapshots on the source
        VM and to create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Start the disposable VM, create two snapshots on it, record the
           snapshot list, and write a unique data marker file into the guest.
           Wait until the VM appears in Forklift inventory and create the
           copy-offload storage Secret.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class with the vsphereXcopyConfig offload plugin, and a
           NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify the transfer method: the populate pod logs report no XCOPY
           (fallback) for the disk with snapshots.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence, disk count, and that the source VM's snapshots are
           unchanged after migration.
        7. Start the migrated VM, read the marker file over SSH and confirm
           its content matches what was written before migration, then stop
           the VM. After the checks, delete the migrated VM; the secret,
           maps, Plan, and source snapshots are removed with the run.

    Expected result:
        The migration succeeds via the fallback transfer, the source
        snapshots are preserved, and the marker data written before migration
        is present and identical on the migrated VM. On failure, inspect the
        migration CR status, the populate pod logs, the source VM's snapshot
        list, and the marker file content on the migrated VM.
    """


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thin_snapshots_migration"])],
    indirect=True,
    ids=["MTV-560:copyoffload-thin-snapshots"],
)
@pytest.mark.copyoffload_snapshots
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadThinSnapshotsMigration(CopyoffloadSnapshotBase):
    """Copy-offload a VM with a thin disk and source snapshots; verify data integrity and snapshot preservation.

    Purpose/Regression:
        Same scenario as the thick-disk snapshot variants, exercised with a
        thin-provisioned disk (MTV-560): source snapshots must not break the
        copy-offload migration, current disk data must arrive intact, and
        the source snapshots must remain unchanged. Snapshots force the
        fallback transfer. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift (XCOPY-capable, e.g. NetApp ONTAP or Hitachi Vantara),
        with storage-vendor credentials for the copy-offload storage Secret.
        Prepare a destination storage class that provisions block volumes
        (volumeMode: Block) and a reachable destination network. Use a
        disposable Linux VM (e.g. cloned from a template) with a
        thin-provisioned disk, guest-agent access, and SSH access for the
        data integrity check. Have permission to create snapshots on the
        source VM and to create secrets, maps, and a Plan in a target
        namespace.

    Test plan:
        1. Start the disposable VM, create two snapshots on it, record the
           snapshot list, and write a unique data marker file into the guest.
           Wait until the VM appears in Forklift inventory and create the
           copy-offload storage Secret.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class with the vsphereXcopyConfig offload plugin, and a
           NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify the transfer method: the populate pod logs report no XCOPY
           (fallback) for the disk with snapshots.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence, disk count, and that the source VM's snapshots are
           unchanged after migration.
        7. Start the migrated VM, read the marker file over SSH and confirm
           its content matches what was written before migration, then stop
           the VM. After the checks, delete the migrated VM; the secret,
           maps, Plan, and source snapshots are removed with the run.

    Expected result:
        The migration succeeds via the fallback transfer, the source
        snapshots are preserved, and the marker data written before migration
        is present and identical on the migrated VM. On failure, inspect the
        migration CR status, the populate pod logs, the source VM's snapshot
        list, and the marker file content on the migrated VM.
    """


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_2tb_vm_snapshots_migration"])],
    indirect=True,
    ids=["MTV-575:copyoffload-2tb-vm-snapshots"],
)
@pytest.mark.copyoffload_snapshots
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffload2TbVmSnapshotsMigration(CopyoffloadSnapshotBase):
    """Copy-offload a VM with a 2 TB disk and source snapshots; verify the large fallback transfer and data integrity.

    Purpose/Regression:
        Copy-offload at scale with snapshots (MTV-575): a VM with a 2 TB
        data disk and two source snapshots must migrate completely via the
        fallback transfer within the migration timeout, with current disk
        data intact and the source snapshots unchanged. There is no induced
        failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift (XCOPY-capable, e.g. NetApp ONTAP or Hitachi Vantara)
        with capacity for a 2 TB disk, with storage-vendor credentials for
        the copy-offload storage Secret. Prepare a destination storage class
        that provisions block volumes (volumeMode: Block) with matching
        capacity and a reachable destination network. Use a disposable Linux
        VM (e.g. cloned from a template) with an added 2 TB thin-provisioned
        data disk, guest-agent access, and SSH access for the data integrity
        check. Have permission to create snapshots on the source VM and to
        create secrets, maps, and a Plan in a target namespace, and allow
        time for the large transfer.

    Test plan:
        1. Start the disposable VM, create two snapshots on it, record the
           snapshot list, and write a unique data marker file into the guest.
           Wait until the VM appears in Forklift inventory and create the
           copy-offload storage Secret.
        2. Create a StorageMap that maps the disks to the block-volume
           storage class with the vsphereXcopyConfig offload plugin, and a
           NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded within the
           migration timeout.
        5. Verify the transfer method: the populate pod logs report no XCOPY
           (fallback) for the disks with snapshots.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence, disk count, and that the source VM's snapshots are
           unchanged after migration.
        7. Start the migrated VM, read the marker file over SSH and confirm
           its content matches what was written before migration, then stop
           the VM. After the checks, delete the migrated VM; the secret,
           maps, Plan, and source snapshots are removed with the run.

    Expected result:
        The large migration succeeds via the fallback transfer, the source
        snapshots are preserved, and the marker data written before migration
        is present and identical on the migrated VM. On failure, inspect the
        migration CR status for timeouts, the populate pod logs, the source
        VM's snapshot list, and the marker file content on the migrated VM.
    """


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thick_lazy_migration"])],
    indirect=True,
    ids=["MTV-580:copyoffload-thick-lazy"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadThickLazyMigration:
    """Copy-offload a VM with a thick lazy-zeroed disk using XCOPY and verify the migrated VM.

    Purpose/Regression:
        Same core copy-offload behavior as the thin-disk scenario, exercised
        with a thick-provisioned lazy-zeroed disk: the disk must transfer via
        hardware-assisted copy (VAAI XCOPY) and the migrated VM must
        validate. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. The VM disk must live on storage shared between
        vSphere and OpenShift that supports hardware-assisted copy (VAAI
        XCOPY, e.g. NetApp ONTAP or Hitachi Vantara), with storage-vendor
        credentials available for the copy-offload storage Secret. Prepare a
        destination storage class that provisions block volumes
        (volumeMode: Block) on that shared storage and a reachable destination
        network. Use a disposable Linux VM (e.g. cloned from a template) with
        a thick lazy-zeroed disk and guest-agent access. Have permission to
        create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with a thick lazy-zeroed disk, wait
           until it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the disk.
        6. Validate the migrated VM: power state, connectivity, and CPU,
           memory, network, and storage equivalence with the source. After
           the checks, delete the migrated VM; the secret, maps, and Plan are
           removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for the thick lazy-zeroed
        disk, and the migrated VM matches the source. On failure, inspect the
        migration CR status for the VM's failed step, the populate pod logs
        for the xcopyUsed value and storage-vendor errors, and the StorageMap
        configuration.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_thick_eager_migration"])],
    indirect=True,
    ids=["MTV-582:copyoffload-thick-eager"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadThickEagerMigration:
    """Copy-offload a VM with a thick eager-zeroed disk using XCOPY and verify the migrated VM.

    Purpose/Regression:
        Same core copy-offload behavior as the thin-disk scenario, exercised
        with a thick-provisioned eager-zeroed disk: the disk must transfer via
        hardware-assisted copy (VAAI XCOPY) and the migrated VM must
        validate. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. The VM disk must live on storage shared between
        vSphere and OpenShift that supports hardware-assisted copy (VAAI
        XCOPY, e.g. NetApp ONTAP or Hitachi Vantara), with storage-vendor
        credentials available for the copy-offload storage Secret. Prepare a
        destination storage class that provisions block volumes
        (volumeMode: Block) on that shared storage and a reachable destination
        network. Use a disposable Linux VM (e.g. cloned from a template) with
        a thick eager-zeroed disk and guest-agent access. Have permission to
        create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with a thick eager-zeroed disk, wait
           until it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the disk.
        6. Validate the migrated VM: power state, connectivity, and CPU,
           memory, network, and storage equivalence with the source. After
           the checks, delete the migrated VM; the secret, maps, and Plan are
           removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for the thick eager-zeroed
        disk, and the migrated VM matches the source. On failure, inspect the
        migration CR status for the VM's failed step, the populate pod logs
        for the xcopyUsed value and storage-vendor errors, and the StorageMap
        configuration.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )


@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_disk_migration"])],
    indirect=True,
    ids=["MTV-561:copyoffload-multi-disk"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadMultiDiskMigration:
    """Copy-offload a multi-disk VM using XCOPY and verify every disk arrives.

    Purpose/Regression:
        Copy-offload must handle a VM with several disks — the template disk
        plus added data disks — transferring each via hardware-assisted copy
        and creating all of them on the destination. The pass signal is XCOPY
        used for every disk plus a disk-count and content check on the
        migrated VM. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. The VM disks must live on storage shared between
        vSphere and OpenShift that supports hardware-assisted copy (VAAI
        XCOPY, e.g. NetApp ONTAP or Hitachi Vantara), with storage-vendor
        credentials available for the copy-offload storage Secret. Prepare a
        destination storage class that provisions block volumes
        (volumeMode: Block) on that shared storage and a reachable destination
        network. Use a disposable Linux VM (e.g. cloned from a template) with
        its template disk plus at least one added persistent data disk (e.g.
        30 GB thick lazy-zeroed), and guest-agent access. Have permission to
        create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the added data disk, wait until it
           appears in Forklift inventory, and create the copy-offload storage
           Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for every disk.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for every disk and the
        migrated VM has the same disk count as the source. On failure,
        inspect the migration CR status for the failed disk step, the
        populate pod logs per disk for the xcopyUsed value, and the migrated
        VM's disk attachments.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_dual_disk_mixed_thin_thick_migration"])],
    indirect=True,
    ids=["MTV-584:copyoffload-dual-disk-mixed-thin-thick"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadDualDiskMixedThinThickMigration:
    """Copy-offload a VM mixing thin and thick-lazy disks and verify both transfer via XCOPY (MTV-584).

    Purpose/Regression:
        A VM whose disks use different provisioning types — one
        thin-provisioned and one thick lazy-zeroed — must have every disk
        accelerated by hardware-assisted copy (VAAI XCOPY), with no disk
        falling back due to the mixed configuration (MTV-584). The pass
        signal is XCOPY used for both disks plus a disk-count and content
        check on the migrated VM. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. The VM disks must live on storage shared between
        vSphere and OpenShift that supports hardware-assisted copy (VAAI
        XCOPY, e.g. NetApp ONTAP or Hitachi Vantara), with storage-vendor
        credentials available for the copy-offload storage Secret. Prepare a
        destination storage class that provisions block volumes
        (volumeMode: Block) on that shared storage and a reachable destination
        network. Use a disposable Linux VM (e.g. cloned from a template) with
        a thin-provisioned template disk plus one added thick lazy-zeroed
        persistent data disk, and guest-agent access. Have permission to
        create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the thin and thick-lazy disks, wait
           until it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps both disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for both disks.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for both the thin and the
        thick-lazy disk, and the migrated VM has the same disk count as the
        source. On failure, inspect the migration CR status for the failed
        disk step, the populate pod logs per disk for the xcopyUsed value,
        and the migrated VM's disk attachments.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_disk_different_path_migration"])],
    indirect=True,
    ids=["MTV-563:copyoffload-multi-disk-different-path"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadMultiDiskDifferentPathMigration:
    """Copy-offload a VM whose added disk lives in a different datastore folder and verify XCOPY still finds it.

    Purpose/Regression:
        Copy-offload disk discovery must locate a disk stored in a different
        folder (path) on the datastore than the VM's default directory — not
        only disks in the VM home folder. The pass signal is XCOPY used for
        every disk plus a disk-count check on the migrated VM. There is no
        induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret. Prepare a destination storage
        class that provisions block volumes (volumeMode: Block) on that shared
        storage and a reachable destination network. Use a disposable Linux
        VM (e.g. cloned from a template) with its template disk plus one
        added persistent data disk placed in a separate datastore folder
        (e.g. a shared disks folder), and guest-agent access. Have permission
        to create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the added data disk in the separate
           datastore folder, wait until it appears in Forklift inventory, and
           create the copy-offload storage Secret with the storage-vendor
           credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for every disk, including
           the one in the different folder.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for the disk in the non-default
        folder, and the migrated VM has the same disk count as the source. On
        failure, inspect the populate pod logs for disk path resolution
        errors, the migration CR status for the failed disk step, and the
        source disk's datastore folder.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.copyoffload_sanity
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_rdm_virtual_disk_migration"])],
    indirect=True,
    ids=["MTV-562:copyoffload-rdm-virtual"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "rdm_config",
    "cleanup_migrated_vms",
)
class TestCopyoffloadRdmVirtualDiskMigration:
    """Copy-offload a VM with a virtual-mode RDM disk and verify it migrates as a SCSI LUN using XCOPY.

    Purpose/Regression:
        A virtual (dependent) Raw Device Mapping disk on XCOPY-capable
        storage must transfer via hardware-assisted copy, and with
        rdmAsLun=true on the Plan it must appear on the destination VM as a
        LUN device on a SCSI bus rather than a regular virtio disk. The pass
        signal is XCOPY used for the disk, the LUN/SCSI device type on the
        migrated VM, and a disk-count check. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret and a LUN available for the RDM
        disk. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) on that shared storage and a reachable
        destination network. Use a disposable Linux VM (e.g. cloned from a
        template) with an added virtual-mode RDM disk backed by the LUN, and
        guest-agent access. Have permission to create secrets, maps, and a
        Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the virtual-mode RDM disk, wait
           until it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload and rdmAsLun
           enabled. Check the Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the RDM disk.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, the same disk count as the source,
           and that the RDM disk is present as a LUN device with SCSI bus
           (regular disks remain virtio). After the checks, delete the
           migrated VM; the secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used, and the migrated VM carries
        the RDM disk as a SCSI-bus LUN with the expected disk count. On
        failure, inspect the populate pod logs for the xcopyUsed value, the
        migrated VM's disk devices for lun.bus=scsi, and the migration CR
        status for the failed disk step.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
            rdm_as_lun=prepared_plan["rdm_as_lun"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )
        verify_rdm_disk_bus_types(destination_provider=destination_provider, plan=prepared_plan)


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_rdm_physical_disk_migration"])],
    indirect=True,
    ids=["MTV-596:copyoffload-rdm-physical"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "rdm_config",
    "cleanup_migrated_vms",
)
class TestCopyoffloadRdmPhysicalDiskMigration:
    """Copy-offload a VM with a physical-mode (compatibility) RDM disk and verify it migrates as a SCSI LUN using XCOPY (MTV-596).

    Purpose/Regression:
        A physical-mode RDM disk (compatibility mode, independent
        persistent) on XCOPY-capable storage must transfer like the
        virtual-mode variant: via hardware-assisted copy, and with
        rdmAsLun=true it must appear on the destination VM as a LUN device on
        a SCSI bus (MTV-596). The pass signal is XCOPY used for the disk,
        the LUN/SCSI device type on the migrated VM, and a disk-count check.
        There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret and a LUN available for the RDM
        disk. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) on that shared storage and a reachable
        destination network. Use a disposable Linux VM (e.g. cloned from a
        template) with an added physical-mode (compatibility mode,
        independent persistent) RDM disk backed by the LUN, and guest-agent
        access. Have permission to create secrets, maps, and a Plan in a
        target namespace.

    Test plan:
        1. Prepare the disposable VM with the physical-mode RDM disk, wait
           until it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload and rdmAsLun
           enabled. Check the Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the RDM disk.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, the same disk count as the source,
           and that the RDM disk is present as a LUN device with SCSI bus
           (regular disks remain virtio). After the checks, delete the
           migrated VM; the secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used, and the migrated VM carries
        the physical-mode RDM disk as a SCSI-bus LUN with the expected disk
        count. On failure, inspect the populate pod logs for the xcopyUsed
        value, the migrated VM's disk devices for lun.bus=scsi, and the
        migration CR status for the failed disk step.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
            rdm_as_lun=prepared_plan["rdm_as_lun"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )
        verify_rdm_disk_bus_types(destination_provider=destination_provider, plan=prepared_plan)


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.warm
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_warm_rdm_virtual_disk_migration"])],
    indirect=True,
    ids=["MTV-597:copyoffload-warm-rdm-virtual"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "precopy_interval_forkliftcontroller",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "rdm_config",
    "cleanup_migrated_vms",
)
class TestCopyoffloadWarmRdmVirtualDiskMigration:
    """Warm copy-offload migration of a running VM with a virtual-mode RDM disk, transferred with XCOPY (MTV-597).

    Purpose/Regression:
        Warm (incremental, with cutover) copy-offload migration must also
        handle a VM with a virtual (dependent) RDM disk attached (MTV-597):
        the incremental passes and cutover must complete with the disk
        transferred via hardware-assisted copy. There is no induced
        failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret and a LUN available for the RDM
        disk. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) on that shared storage and a reachable
        destination network. Use a disposable Linux VM (e.g. cloned from a
        template), powered on, with an added virtual-mode RDM disk backed by
        the LUN, and guest-agent access. Have permission to create secrets,
        maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable powered-on VM with the virtual-mode RDM
           disk, wait until it appears in Forklift inventory, and create the
           copy-offload storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a warm Plan for the VM with copy-offload enabled. Check
           the Plan's Ready condition is True.
        4. Run the warm migration with a cutover and check it reaches
           Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the RDM disk.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and the same disk count as the
           source. After the checks, delete the migrated VM; the secret,
           maps, and Plan are removed with the run.

    Expected result:
        The warm migration completes through cutover with XCOPY used, and the
        migrated VM has the expected disk count and validates against
        the source. On failure, inspect the migration CR status for the
        failed step (preflight or cutover), the populate pod logs for the
        xcopyUsed value, and the migrated VM's disk devices.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute warm migration with cutover."""
        LOGGER.info("Executing warm copy-offload migration with virtual RDM disk")
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.copyoffload_sanity
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_datastore_migration"])],
    indirect=True,
    ids=["MTV-564:copyoffload-multi-datastore"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "multi_datastore_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadMultiDatastoreMigration:
    """Copy-offload a VM whose disks sit on two different XCOPY-capable datastores and verify XCOPY is used for all.

    Purpose/Regression:
        Copy-offload must handle a VM whose disks are spread across multiple
        XCOPY-capable datastores, not only a single datastore: every disk
        must transfer via hardware-assisted copy. The pass signal is XCOPY
        used for every disk plus a disk-count check on the migrated VM.
        There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Have at least two datastores with storage shared
        between vSphere and OpenShift that supports hardware-assisted copy
        (VAAI XCOPY, e.g. NetApp ONTAP or Hitachi Vantara), with
        storage-vendor credentials available for the copy-offload storage
        Secret. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) on that shared storage and a reachable
        destination network. Use a disposable Linux VM (e.g. cloned from a
        template) with its template disk on one datastore and one added
        persistent data disk on a second datastore, and guest-agent access.
        Have permission to create secrets, maps, and a Plan in a target
        namespace.

    Test plan:
        1. Prepare the disposable VM with disks on both datastores, wait
           until it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps disks on both datastores to the
           block-volume storage class and carries the vsphereXcopyConfig
           offload plugin. Create a NetworkMap for the VM's network. Confirm
           both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for every disk on both
           datastores.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for disks on both datastores,
        and the migrated VM has the same disk count as the source. On
        failure, inspect the populate pod logs per disk (correlated with the
        datastore names) for the xcopyUsed value, the migration CR status,
        and the StorageMap's datastore mappings.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration for multiple datastores."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        secondary_datastore_id = copyoffload_config_data["secondary_datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Multi-datastore migration using primary datastore: %s", datastore_id)
        LOGGER.info("Multi-datastore migration using secondary datastore: %s", secondary_datastore_id)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            secondary_datastore_id=secondary_datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_multi_disk_different_datastore_path_migration"])],
    indirect=True,
    ids=["MTV-595:copyoffload-multi-disk-different-datastore-path"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "multi_datastore_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadMultiDiskDifferentDatastorePathMigration:
    """Copy-offload a VM whose added disk lives on another datastore in a different folder (MTV-595).

    Purpose/Regression:
        Copy-offload disk discovery must locate a disk that differs from the
        template disk in both datastore and folder (path), combining the
        multi-datastore and different-path conditions (MTV-595). The pass
        signal is XCOPY used for every disk plus a disk-count check on the
        migrated VM. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Have at least two datastores with storage shared
        between vSphere and OpenShift that supports hardware-assisted copy
        (VAAI XCOPY, e.g. NetApp ONTAP or Hitachi Vantara), with
        storage-vendor credentials available for the copy-offload storage
        Secret. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) on that shared storage and a reachable
        destination network. Use a disposable Linux VM (e.g. cloned from a
        template) with its template disk on one datastore and one added
        persistent data disk on a second datastore placed in a separate
        folder, and guest-agent access. Have permission to create secrets,
        maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the added disk on the second
           datastore in the separate folder, wait until it appears in
           Forklift inventory, and create the copy-offload storage Secret
           with the storage-vendor credentials.
        2. Create a StorageMap that maps disks on both datastores to the
           block-volume storage class and carries the vsphereXcopyConfig
           offload plugin. Create a NetworkMap for the VM's network. Confirm
           both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for every disk, including
           the one on the other datastore and folder.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for the disk on the different
        datastore and folder, and the migrated VM has the same disk count as
        the source. On failure, inspect the populate pod logs for disk path
        resolution errors, the migration CR status, and the source disk's
        datastore and folder placement.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration for multiple datastores."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        secondary_datastore_id = copyoffload_config_data["secondary_datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Primary datastore: %s", datastore_id)
        LOGGER.info("Secondary datastore (custom path disk): %s", secondary_datastore_id)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            secondary_datastore_id=secondary_datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.copyoffload_sanity
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_mixed_datastore_migration"])],
    indirect=True,
    ids=["MTV-565:copyoffload-mixed-datastore"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "mixed_datastore_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadMixedDatastoreMigration:
    """Copy-offload a VM with disks on one XCOPY-capable and one non-XCOPY datastore and verify per-disk transfer method.

    Purpose/Regression:
        When only some of a VM's disks are on XCOPY-capable storage, the
        XCOPY-capable disk must use hardware-assisted copy while the disk on
        the non-XCOPY datastore must fall back to the standard transfer —
        per disk, not per VM. The pass signal is the xcopyUsed value in the
        populate pod logs matching each disk's datastore, plus a disk-count
        check on the migrated VM. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Have one datastore shared between vSphere and
        OpenShift that supports hardware-assisted copy (VAAI XCOPY, e.g.
        NetApp ONTAP or Hitachi Vantara) and one shared or accessible
        datastore without XCOPY support, with storage-vendor credentials
        available for the copy-offload storage Secret. Prepare a destination
        storage class that provisions block volumes (volumeMode: Block) and a
        reachable destination network. Use a disposable Linux VM (e.g.
        cloned from a template) with its template disk on the XCOPY-capable
        datastore and one added data disk on the non-XCOPY datastore, and
        guest-agent access. Have permission to create secrets, maps, and a
        Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with disks on both datastores, wait
           until it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps both datastores to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify the transfer method per disk: the populate pod logs and
           Plan task annotations report xcopyUsed for the disk on the
           XCOPY-capable datastore and no XCOPY for the disk on the
           non-XCOPY datastore, with each datastore represented.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with the XCOPY-capable disk using XCOPY and
        the non-XCOPY datastore disk falling back, and the migrated VM has
        the same disk count as the source. On failure, inspect the populate
        pod logs per disk (correlated with the datastore names) for the
        xcopyUsed value and the migration CR status.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration for mixed datastores."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        non_xcopy_datastore_id = copyoffload_config_data["non_xcopy_datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Mixed datastore migration using XCOPY datastore: %s", datastore_id)
        LOGGER.info("Mixed datastore migration using non-XCOPY datastore: %s", non_xcopy_datastore_id)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            non_xcopy_datastore_id=non_xcopy_datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan["copyoffload"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        source_provider: VMWareProvider,
        source_provider_data: dict[str, Any],
        fixture_store: dict[str, Any],
    ) -> None:
        """Verify XCOPY per disk: XCOPY-capable datastore uses XCOPY, non-XCOPY uses fallback.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            source_provider (VMWareProvider): Source VMware provider for datastore name lookup.
            source_provider_data (dict[str, Any]): Source provider configuration.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        copyoffload_config_data: dict[str, Any] = source_provider_data["copyoffload"]
        xcopy_datastore_id: str = copyoffload_config_data["datastore_id"]
        non_xcopy_datastore_id: str = copyoffload_config_data["non_xcopy_datastore_id"]
        datastore_names_by_id: dict[str, str] = {
            xcopy_datastore_id: source_provider.get_datastore_name_by_id(xcopy_datastore_id),
            non_xcopy_datastore_id: source_provider.get_datastore_name_by_id(non_xcopy_datastore_id),
        }

        verify_xcopy_used_per_datastore(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_by_datastore_id={
                xcopy_datastore_id: True,
                non_xcopy_datastore_id: False,
            },
            datastore_names_by_id=datastore_names_by_id,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_fallback_large_migration"])],
    indirect=True,
    ids=["MTV-614:copyoffload-fallback-large"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "mixed_datastore_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadFallbackLargeMigration:
    """Copy-offload a large-disk VM on a non-XCOPY datastore and verify it completes via fallback transfer.

    Purpose/Regression:
        When a VM's disks live on a datastore without XCOPY (VAAI) support,
        copy-offload must fall back to the standard transfer method and still
        complete, including for large disks. The pass signal is the
        migration succeeding with no XCOPY reported, plus a disk-count check
        on the migrated VM. There is no induced failure — the fallback itself
        is the behavior under test.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Have a datastore accessible to the migration
        without XCOPY support and with capacity for large disks (e.g. a
        100 GB added disk), with storage-vendor credentials available for
        the copy-offload storage Secret. Prepare a destination storage class
        that provisions block volumes (volumeMode: Block) and a reachable
        destination network. Use a disposable Linux VM (e.g. cloned from a
        template) with its template disk relocated to the non-XCOPY datastore
        and one added large data disk on the same datastore, and guest-agent
        access. Have permission to create secrets, maps, and a Plan in a
        target namespace, and allow time for the large transfer.

    Test plan:
        1. Prepare the disposable VM with both disks on the non-XCOPY
           datastore, wait until it appears in Forklift inventory, and create
           the copy-offload storage Secret with the storage-vendor
           credentials.
        2. Create a StorageMap that maps the disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded despite the
           datastore lacking XCOPY support.
        5. Verify the fallback was used: the populate pod logs report no
           XCOPY (xcopyUsed=0) for the disks.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration of the large-disk VM completes successfully using the
        fallback transfer with no XCOPY reported, and the migrated VM has
        the same disk count as the source. On failure, inspect the migration
        CR status for the failed disk step, the populate pod logs, and the
        datastore's VAAI capability.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict,
        fixture_store: dict,
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict,
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration for non-XCOPY datastore.

        Args:
            prepared_plan: Prepared plan configuration with VM details.
            fixture_store: Shared fixture storage for test resources.
            ocp_admin_client: Kubernetes dynamic client for API operations.
            source_provider: Source provider instance (VMware/RHV/etc).
            destination_provider: Destination provider instance (OpenShift).
            source_provider_inventory: Forklift inventory for source provider.
            target_namespace: Target namespace for migration.
            source_provider_data: Provider configuration data.
            copyoffload_storage_secret: Secret resource for copy-offload storage access.

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails.
        """
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        non_xcopy_datastore_id = copyoffload_config_data["non_xcopy_datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Non-XCOPY large VM migration test - 2 disks on non-VAAI datastore")
        LOGGER.info(f"XCOPY datastore (for storage map): {datastore_id}")
        LOGGER.info(f"Non-XCOPY datastore (VM disks location): {non_xcopy_datastore_id}")
        LOGGER.info("VM configuration: template disk + 100GB added disk (both on non_xcopy_datastore)")
        LOGGER.info("Testing fallback behavior for large VM with disks on non-VAAI datastore")

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            non_xcopy_datastore_id=non_xcopy_datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict,
        fixture_store: dict,
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource.

        Args:
            prepared_plan: Prepared plan configuration with VM details.
            fixture_store: Shared fixture storage for test resources.
            ocp_admin_client: Kubernetes dynamic client for API operations.
            source_provider: Source provider instance (VMware/RHV/etc).
            destination_provider: Destination provider instance (OpenShift).
            source_provider_inventory: Forklift inventory for source provider.
            target_namespace: Target namespace for migration.
            multus_network_name: Dictionary with Multus network configuration (name and namespace).

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails.
        """
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict,
        fixture_store: dict,
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource.

        Args:
            prepared_plan: Prepared plan configuration with VM details.
            fixture_store: Shared fixture storage for test resources.
            ocp_admin_client: Kubernetes dynamic client for API operations.
            source_provider: Source provider instance (VMware/RHV/etc).
            destination_provider: OpenShift provider instance.
            target_namespace: Target namespace for migration.
            source_provider_inventory: Forklift inventory for source provider.

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails.
        """
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan["copyoffload"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute migration.

        Args:
            fixture_store: Shared fixture storage for test resources.
            ocp_admin_client: Kubernetes dynamic client for API operations.
            target_namespace: Target namespace for migration.

        Returns:
            None

        Raises:
            AssertionError: If migration execution fails.
        """
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY fallback was used (no XCOPY acceleration).

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=False,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan: dict,
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_data: dict,
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager,
    ) -> None:
        """Validate migrated VMs and verify disk count.

        Args:
            prepared_plan: Prepared plan configuration with VM details.
            source_provider: Source provider instance (VMware/RHV/etc).
            destination_provider: Destination provider instance (OpenShift).
            source_provider_data: Provider configuration data.
            target_namespace: Target namespace for migration.
            source_vms_namespace: Source VMs namespace.
            source_provider_inventory: Forklift inventory for source provider.
            vm_ssh_connections: SSH connection manager for VM validation.

        Returns:
            None

        Raises:
            AssertionError: If VM validation or disk count verification fails.
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_independent_persistent_disk_migration"])],
    indirect=True,
    ids=["MTV-567:copyoffload-independent-persistent"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadIndependentPersistentDiskMigration:
    """Copy-offload a VM with an independent-persistent disk and verify XCOPY and disk arrival.

    Purpose/Regression:
        A disk configured as independent persistent (excluded from source
        snapshots) must still be migrated by copy-offload via
        hardware-assisted copy and created on the destination. The pass
        signal is XCOPY used for every disk plus a disk-count check on the
        migrated VM. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret. Prepare a destination storage
        class that provisions block volumes (volumeMode: Block) and a
        reachable destination network. Use a disposable Linux VM (e.g.
        cloned from a template) with an added independent-persistent data
        disk, and guest-agent access. Have permission to create secrets,
        maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the independent-persistent disk,
           wait until it appears in Forklift inventory, and create the
           copy-offload storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for every disk.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for the independent-persistent
        disk, and the migrated VM has the same disk count as the source. On
        failure, inspect the migration CR status for the failed disk step,
        the populate pod logs for the xcopyUsed value, and the migrated VM's
        disk attachments.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_independent_nonpersistent_disk_migration"])],
    indirect=True,
    ids=["MTV-568:copyoffload-independent-nonpersistent"],
)
@pytest.mark.usefixtures(
    "nonpersistent_disk_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadIndependentNonpersistentDiskMigration:
    """Copy-offload a VM with an independent-nonpersistent disk and verify XCOPY and disk arrival.

    Purpose/Regression:
        A disk configured as independent nonpersistent (excluded from source
        snapshots, changes discarded on power cycle) must still be migrated
        by copy-offload via hardware-assisted copy and created on the
        destination. The pass signal is XCOPY used for every disk plus a
        disk-count check on the migrated VM. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret. Prepare a destination storage
        class that provisions block volumes (volumeMode: Block) and a
        reachable destination network. Use a disposable Linux VM (e.g.
        cloned from a template) with an added independent-nonpersistent data
        disk, and guest-agent access. Have permission to create secrets,
        maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the independent-nonpersistent
           disk, wait until it appears in Forklift inventory, and create the
           copy-offload storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for every disk.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for the
        independent-nonpersistent disk, and the migrated VM has the same
        disk count as the source. On failure, inspect the migration CR status
        for the failed disk step, the populate pod logs for the xcopyUsed
        value, and the migrated VM's disk attachments.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.copyoffload_sanity
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_10_mixed_disks_migration"])],
    indirect=True,
    ids=["MTV-573:copyoffload-10-mixed-disks"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffload10MixedDisksMigration:
    """Copy-offload a VM with ten disks of mixed thin and thick provisioning and verify all arrive via XCOPY.

    Purpose/Regression:
        Copy-offload must scale to a VM with many disks of mixed provisioning
        types — ten disks alternating thin and thick lazy-zeroed —
        transferring each via hardware-assisted copy and creating all of them
        on the destination. The pass signal is XCOPY used for every disk plus
        a disk-count check on the migrated VM. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret and capacity for ten additional
        disks. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) and a reachable destination network. Use
        a disposable Linux VM (e.g. cloned from a template) with ten added
        persistent data disks alternating thin and thick lazy-zeroed (e.g.
        10 GB each), and guest-agent access. Have permission to create
        secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the ten added disks, wait until it
           appears in Forklift inventory, and create the copy-offload storage
           Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for every disk.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM (eleven disks including the template disk). After
           the checks, delete the migrated VM; the secret, maps, and Plan
           are removed with the run.

    Expected result:
        The migration succeeds with XCOPY used for all ten mixed disks, and
        the migrated VM has the same disk count as the source. On failure,
        inspect the migration CR status for the failed disk step, the
        populate pod logs per disk for the xcopyUsed value, and the migrated
        VM's disk attachments.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_large_vm_migration"])],
    indirect=True,
    ids=["MTV-600:copyoffload-large-vm"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadLargeVmMigration:
    """Copy-offload a VM with a 1 TB data disk and verify the large transfer completes via XCOPY.

    Purpose/Regression:
        Copy-offload must handle a very large disk (1 TB): the
        hardware-assisted copy path must complete the transfer within the
        migration timeout and create the disk on the destination. The pass
        signal is XCOPY used for the disk plus a disk-count check on the
        migrated VM. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift, support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), and have capacity for a 1 TB disk, with
        storage-vendor credentials available for the copy-offload storage
        Secret. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) with matching capacity and a reachable
        destination network. Use a disposable Linux VM (e.g. cloned from a
        template) with an added 1 TB thin-provisioned data disk, and
        guest-agent access. Have permission to create secrets, maps, and a
        Plan in a target namespace, and allow time for the large transfer.

    Test plan:
        1. Prepare the disposable VM with the 1 TB data disk, wait until it
           appears in Forklift inventory, and create the copy-offload storage
           Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded within the
           migration timeout.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the large disk.
        6. Validate the migrated VM: power state, connectivity, resource
           equivalence with the source, and that the number of disks matches
           the source VM. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration of the 1 TB disk completes successfully with XCOPY
        used, and the migrated VM has the same disk count as the source. On
        failure, inspect the migration CR status for the failed disk step
        and timeouts, the populate pod logs, and the destination storage
        class capacity.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_populator_throttling_migration"])],
    indirect=True,
    ids=["MTV-696:copyoffload-populator-throttling"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "populator_inflight_forkliftcontroller",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadPopulatorThrottlingMigration:
    """Copy-offload with per-ESXi-host populate pod throttling: peak concurrency respects the configured limit (MTV-696).

    Purpose/Regression:
        The ForkliftController's per-ESXi-host populator limit
        (controller_max_populator_inflight) must cap concurrently running
        populate pods per source host while still migrating every disk
        (MTV-696, covering MTV-5654 scenarios 1 and 3). The failure path is
        a deliberately low limit (2) against a VM with five disks on a single
        ESXi host: throttling must hold the peak at the limit, emit
        PopulatorThrottled events for the queued disks, and label populate
        pods with their source host. The migration must still complete.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY), with
        storage-vendor credentials available for the copy-offload storage
        Secret. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) and a reachable destination network. Use
        a disposable Linux VM (e.g. cloned from a template) with its template
        disk plus four added data disks, all disks reachable from one ESXi
        host, and guest-agent access. Have permission to patch the
        ForkliftController (to set and later restore the populator limit),
        create secrets, maps, and a Plan in a target namespace. The limit is
        restored to its original value after the scenario.

    Test plan:
        1. Set the ForkliftController's per-host populator limit to 2 and
           record the previous value. Prepare the disposable five-disk VM,
           wait until it appears in Forklift inventory, and create the
           copy-offload storage Secret.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class with the vsphereXcopyConfig offload plugin, and a
           NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration while monitoring populate pods, and check it
           reaches Succeeded.
        5. Verify throttling from the monitoring samples: the peak number of
           concurrently active populate pods per ESXi host never exceeds the
           limit of 2 and reaches min(limit, disk count); populate pods
           carry a source host label; and at least (disk count - limit)
           PVCs recorded a structured PopulatorThrottled event while queued.
        6. Verify XCOPY was used for every disk, then validate the migrated
           VM: power state, connectivity, resource equivalence, and disk
           count. After the checks, delete the migrated VM, restore the
           ForkliftController limit, and remove the secret, maps, and Plan.

    Expected result:
        The migration completes with throttling observable: peak per-host
        concurrency stays at or below 2, PopulatorThrottled events appear on
        the queued PVCs, and populate pods carry source host labels. On
        failure, inspect the PopulatorThrottled events on the PVCs, the
        populate pod labels and logs, the ForkliftController's configured
        limit, and the migration CR status.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
    max_concurrent_by_host: dict[str, int]

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan["copyoffload"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute migration while monitoring populator concurrency per ESXi host."""
        self.__class__.max_concurrent_by_host = execute_migration_monitoring_populator_inflight(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            max_populator_inflight=POPULATOR_INFLIGHT_LIMIT,
        )

    def test_verify_populator_throttling(
        self,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        fixture_store: dict[str, Any],
    ) -> None:
        """Verify sourceHost labels, PopulatorThrottled events, and observed concurrency.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods and PVCs exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_populator_throttling(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            max_concurrent_by_host=self.max_concurrent_by_host,
            fixture_store=fixture_store,
            max_populator_inflight=POPULATOR_INFLIGHT_LIMIT,
        )

    def test_check_xcopy_used(
        self,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        fixture_store: dict[str, Any],
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


class CopyoffloadDedicatedMigrationHostBase:
    """Shared resource creation and verification for dedicated-host scenarios."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
    max_concurrent_by_host: dict[str, int]

    def test_verify_source_vm_placement(
        self,
        prepared_plan: dict[str, Any],
        source_provider_inventory: ForkliftInventory,
        configured_dedicated_hosts: list[str],
    ) -> None:
        """Verify source VM placement matches the selected dedicated-host scenario.

        Args:
            prepared_plan (dict[str, Any]): Plan with cloned source VMs.
            source_provider_inventory (ForkliftInventory): Source inventory with VM host IDs.
            configured_dedicated_hosts (list[str]): Configured dedicated host IDs.
        """
        vms = prepared_plan["virtual_machines"]
        assert len(vms) == 2, f"Dedicated-host coverage requires exactly two VMs; found {len(vms)}"
        dedicated_host_ids = set(configured_dedicated_hosts)
        assert dedicated_host_ids, "At least one dedicated ESXi host must be configured"
        first_vm_is_dedicated = prepared_plan.get("pin_first_vm_to_dedicated_host", False)
        if first_vm_is_dedicated:
            assert len(dedicated_host_ids) == 1, (
                "Shared-populator-limit coverage requires exactly one distinct dedicated ESXi host; "
                f"found {sorted(dedicated_host_ids)}"
            )

        for vm_index, vm in enumerate(vms):
            vm_name = vm["name"]
            actual_host_id = get_vm_esxi_host(source_provider_inventory.get_vm(vm_name), vm_name)
            expected_dedicated = first_vm_is_dedicated and vm_index == 0
            is_dedicated = actual_host_id in dedicated_host_ids
            assert is_dedicated == expected_dedicated, (
                f"VM '{vm_name}' is registered on ESXi host '{actual_host_id}'; "
                f"expected {'dedicated' if expected_dedicated else 'non-dedicated'} placement "
                f"relative to configured dedicated hosts {sorted(dedicated_host_ids)}"
            )

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
        configured_dedicated_hosts: list[str],
    ) -> None:
        """Create StorageMap with copy-offload dedicatedMigrationHosts configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
                "dedicatedMigrationHosts": configured_dedicated_hosts,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan["copyoffload"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        prepared_plan: dict[str, Any],
        configured_dedicated_hosts: list[str],
    ) -> None:
        """Execute migration while monitoring active populate pods per runtime host."""
        contention_runtime_host: str | None = None
        if prepared_plan.get("require_cross_source_contention", False):
            contention_runtime_host = configured_dedicated_hosts[0]

        self.__class__.max_concurrent_by_host = execute_migration_monitoring_populator_inflight(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            max_populator_inflight=prepared_plan.get("populator_inflight_limit", POPULATOR_INFLIGHT_LIMIT),
            host_label=THROTTLE_HOST_LABEL,
            include_pending=True,
            use_runtime_host=True,
            contention_runtime_host=contention_runtime_host,
        )

    def test_verify_dedicated_migration_host(
        self,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        fixture_store: dict[str, Any],
        configured_dedicated_hosts: list[str],
        prepared_plan: dict[str, Any],
    ) -> None:
        """Verify XCOPY executed on configured dedicated hosts, not VMs' registered hosts.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods and PVCs exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
            configured_dedicated_hosts (list[str]): Configured dedicatedMigrationHosts.
            prepared_plan (dict[str, Any]): Plan configuration with the populator limit and
                shared-host contention requirement.
        """
        verify_dedicated_migration_host(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            dedicated_hosts=configured_dedicated_hosts,
            max_concurrent_by_host=self.max_concurrent_by_host,
            fixture_store=fixture_store,
            max_populator_inflight=prepared_plan.get("populator_inflight_limit", POPULATOR_INFLIGHT_LIMIT),
            require_cross_source_contention=prepared_plan.get("require_cross_source_contention", False),
        )

    def test_check_xcopy_used(
        self,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        fixture_store: dict[str, Any],
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_dedicated_migration_host_migration"])],
    indirect=True,
    ids=["MTV-4494:copyoffload-dedicated-migration-host"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "populator_inflight_forkliftcontroller",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadDedicatedMigrationHost(CopyoffloadDedicatedMigrationHostBase):
    """Verify dedicated-host routing while every source VM runs elsewhere.

    Purpose/Regression:
        MTV-4494 routes copy-offload disks to configured dedicated ESXi hosts. This
        scenario confirms that every disk uses a configured host and cannot pass by
        coincidence because a source VM already runs on that host.

    Prerequisites:
        Register a vSphere source and OpenShift destination with accessible network
        and XCOPY-capable shared storage. Provide disposable source VMs or templates,
        the copy-offload storage secret, and permission to change the ForkliftController
        populator limit. Configure at least one dedicated ESXi host and a separate
        non-dedicated host. Prevent DRS from moving cloned VMs during the check.

    Test plan:
        1. Clone two VMs and pin both to a non-dedicated host; verify their registered
           host IDs are outside the configured dedicated-host set.
        2. Create an XCOPY StorageMap using the configured host IDs, a NetworkMap, and
           a cold Plan for both VMs. Confirm the Plan is Ready.
        3. Migrate both VMs and confirm every populate pod uses a configured dedicated
           host and no observed host exceeds the configured populator limit. With one
           dedicated host, verify the observed active-pod peak (Pending and Running); with
           multiple configured hosts, host selection is random per disk and only the
           per-host ceiling is required.
        4. Confirm all disks used XCOPY and both migrated VMs pass connectivity and
           disk-count checks. Remove migration resources, destination VMs and PVCs,
           disposable source clones, generated secrets and network attachments, and
           restore the prior controller limit.

    Expected result:
        Migration succeeds and each populate pod is routed to a configured host that
        differs from both VMs' registered hosts. The observed active-pod peak (Pending
        and Running) stays within the configured per-host limit.
    """


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_dedicated_host_shared_populator_limit_migration"])],
    indirect=True,
    ids=["MTV-4494:dedicated-host-shared-populator-limit"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "vm_populator_inflight_forkliftcontroller",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadDedicatedHostSharedPopulatorLimit(CopyoffloadDedicatedMigrationHostBase):
    """Verify MTV-4494: VMs from different source hosts share the dedicated host's populator limit.

    Purpose/Regression:
        A host-counting regression can let disks from VMs registered on different
        source hosts bypass the same dedicated host's populator limit. This scenario
        routes disks from one VM on the dedicated host and one VM elsewhere to that
        single host, then monitors active populate pods against its configured limit.

    Prerequisites:
        Register a vSphere source and OpenShift destination with accessible network
        and XCOPY-capable shared storage. Prepare two disposable Linux VMs or clones
        with three disks each (one template disk and two added disks) on that storage.
        Configure exactly one dedicated ESXi host and at least one other ESXi host.
        Provide the copy-offload secret and permissions to patch the ForkliftController and its
        VM/populator deployments. The test changes and restores the VM and populator limits.
        Use an isolated migration namespace and prevent DRS from moving the VMs during the check.

    Test plan:
        1. Set the VM in-flight limit to 2 and the per-host populator limit to 1. Clone
           one VM onto the dedicated host and the other onto a non-dedicated host;
           confirm both source host IDs.
        2. Create an XCOPY StorageMap using the dedicated host, a NetworkMap, and a
           cold Plan for both VMs. Confirm the Plan is Ready.
        3. Migrate both VMs. Confirm each populate pod's runtime-host metadata, worker
           argument, and logs identify the dedicated host. Prefer throttleHost when
           present; otherwise use --migration-host (the current controller may write the
           runtime host to sourceHost). Verify the observed active-pod peak never exceeds 1.
           During one poll, require a still-Pending PVC with a structured
           PopulatorThrottled event, plus a Running worker from the other source host.
           The sole configured dedicated host is the shared runtime host. Match the Event
           to the current PVC by UID and require its timestamp to be at or after the other
           worker's start time.
        4. Confirm all disks used XCOPY and both migrated VMs pass connectivity and
           disk-count checks. Remove migration resources, destination VMs and PVCs,
           disposable source clones, generated secrets and network attachments, and
           restore the prior controller limit.

    Expected result:
        Migration succeeds with disks from both original hosts executing on the sole
        dedicated host under one shared limit. If routing or throttling fails, inspect
        populate pod labels, worker args, PVC events, and Plan status.
    """


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_dedicated_migration_host_invalid_id_migration"])],
    indirect=True,
    ids=["MTV-4494:copyoffload-dedicated-migration-host-invalid-id"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadDedicatedMigrationHostInvalidId:
    """An invalid dedicatedMigrationHosts ESXi host ID must fail the populate pod with a clear error (MTV-4494).

    Purpose/Regression:
        A StorageMap's dedicated migration host list referencing an ESXi host
        ID that does not exist in vCenter must not silently succeed. There is
        no admission-time validation of the ID, so the failure path is at
        runtime: the Plan and Migration CRs are created, the populate pod
        starts, and fails with an error naming the missing host. The pass
        signal is that failure message, not a successful migration.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY), with
        storage-vendor credentials available for the copy-offload storage
        Secret. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) and a reachable destination network. Use
        a disposable Linux VM (e.g. cloned from a template) with a data disk
        and guest-agent access. Identify an ESXi host ID that does not exist
        in the vCenter inventory (e.g. an unused or malformed ID). Have
        permission to create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM, wait until it appears in Forklift
           inventory, and create the copy-offload storage Secret with the
           storage-vendor credentials.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class, carries the vsphereXcopyConfig offload plugin, and
           lists the non-existent ESXi host ID as a dedicated migration host.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True — the invalid ID is not rejected at
           admission.
        4. Run the migration and check that it fails, and that the failing
           populate pod's logs contain an error naming the invalid host ID
           (no host with that ID was found). After the check, the failed
           migration resources, secret, maps, and Plan are removed with the
           run.

    Expected result:
        The migration fails at the populate stage with a populate pod error
        naming the invalid host ID. On failure of the expectation (migration
        succeeds or the error is missing), inspect the populate pod logs and
        events in the target namespace and the StorageMap's dedicated
        migration host list.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
    invalid_host_id: str

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with an invalid dedicatedMigrationHosts entry."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]
        self.__class__.invalid_host_id = resolve_invalid_dedicated_host_id(
            source_provider_inventory=source_provider_inventory
        )

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
                "dedicatedMigrationHosts": [self.invalid_host_id],
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan["copyoffload"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute migration — expects failure due to an invalid dedicated host ID.

        Args:
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
        """
        with pytest.raises(MigrationPlanExecError):
            execute_copyoffload_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.plan_resource,
                target_namespace=target_namespace,
            )
        verify_populate_pod_failure_reason(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            fixture_store=fixture_store,
            expected_error_substring=f"didn't find any host with id {self.invalid_host_id}",
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_nonconforming_name_migration"])],
    indirect=True,
    ids=["MTV-579:copyoffload-nonconforming-name"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadNonconformingNameMigration:
    """Copy-offload a VM whose name violates Kubernetes naming rules and verify the destination VM gets a sanitized name (MTV-579).

    Purpose/Regression:
        A source VM name containing characters that are invalid in
        Kubernetes names (capital letters, underscores) must not break the
        migration: the destination VM must be created with a sanitized,
        Kubernetes-compliant name (lowercase, underscores converted to
        hyphens), while the migration itself completes normally (MTV-579).
        The pass signal is the destination VM existing under the sanitized
        name. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret. Prepare a destination storage
        class that provisions block volumes (volumeMode: Block) and a
        reachable destination network. Use a disposable Linux VM (e.g.
        cloned from a template) whose clone name deliberately contains
        capital letters and underscores (e.g. ``XCopy_Test_VM_CAPS``), with
        guest-agent access. Have permission to create secrets, maps, and a
        Plan in a target namespace.

    Test plan:
        1. Prepare the disposable VM with the non-conforming name, wait until
           it appears in Forklift inventory, and create the copy-offload
           storage Secret with the storage-vendor credentials.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a cold Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the disk.
        6. Validate the destination: the migrated VM exists with the source
           name converted to a valid Kubernetes name (lowercase, hyphens),
           passes power state, connectivity, resource equivalence, and
           disk-count checks. After the checks, delete the migrated VM; the
           secret, maps, and Plan are removed with the run.

    Expected result:
        The migration succeeds and the destination VM is created under the
        sanitized name (e.g. ``xcopy-test-vm-caps``) with working content.
        On failure, inspect the migration CR status, the destination
        namespace for a VM whose name was not sanitized, and the forklift
        controller logs for name-handling errors.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Starting copy-offload migration test with non-conforming VM name")
        vm_cfg = prepared_plan["virtual_machines"][0]
        source_vm_name = vm_cfg.get("clone_name") or vm_cfg["name"]
        LOGGER.info("Source VM name: %s (contains capitals and underscores)", source_vm_name)
        LOGGER.info("Datastore: %s, Storage vendor: %s", datastore_id, storage_vendor_product)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        LOGGER.info("Executing copy-offload migration with non-conforming name")
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify name sanitization."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )

        # Verify that the destination VM was created with the sanitized Kubernetes-compliant name.
        # This is an explicit test of the name sanitization behavior, separate from the general
        # VM validation done by check_vms(). We need this additional check to verify the specific
        # test scenario: that non-conforming names (capitals, underscores) are properly sanitized.
        vm_cfg = prepared_plan["virtual_machines"][0]
        # vm_cfg["name"] contains the cloned VM name (mutated by prepared_plan fixture).
        # source_vms_data is keyed by this same name to store the provider API object.
        provider_vm_api = prepared_plan["source_vms_data"][vm_cfg["name"]]["provider_vm_api"]
        actual_source_vm_name = provider_vm_api.name
        # MTV should sanitize the source VM name to create the destination VM name
        expected_destination_name = sanitize_kubernetes_name(actual_source_vm_name)
        LOGGER.info(
            "Verifying destination VM name sanitization: '%s' -> '%s'",
            actual_source_vm_name,
            expected_destination_name,
        )

        # Use destination_provider to verify the VM exists with the sanitized name
        destination_vm = destination_provider.vm_dict(
            name=expected_destination_name,  # Use sanitized name to look up the VM
            namespace=target_namespace,
        )

        # Assert the VM exists and has the expected sanitized name
        actual_destination_name = destination_vm["name"]
        assert actual_destination_name == expected_destination_name, (
            f"Destination VM name mismatch!\n"
            f"  Source VM name: '{actual_source_vm_name}'\n"
            f"  Expected sanitized name: '{expected_destination_name}'\n"
            f"  Actual destination name: '{actual_destination_name}'\n"
            f"  This indicates the MTV operator did not properly sanitize the VM name."
        )
        LOGGER.info("✓ Destination VM name correctly sanitized to: '%s'", actual_destination_name)

        # Verify that the VM disk was migrated successfully
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.warm
@pytest.mark.copyoffload_sanity
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_warm_migration"])],
    indirect=True,
    ids=["MTV-577:copyoffload-warm"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "precopy_interval_forkliftcontroller",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadWarmMigration:
    """Warm copy-offload migration of a running VM with incremental transfer and cutover.

    Purpose/Regression:
        Copy-offload must work in a warm migration: the running VM's disks
        are copied incrementally while the source stays up, and a cutover
        finalizes the migration. The pass signal is a successful warm
        migration with XCOPY used for the disk. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret. Prepare a destination storage
        class that provisions block volumes (volumeMode: Block) and a
        reachable destination network. Use a disposable, powered-on Linux
        VM (e.g. cloned from a template) with a thin-provisioned disk and
        guest-agent access, whose disk changes can be captured by
        incremental transfer (e.g. enabled changed-block tracking). Have
        permission to create secrets, maps, and a Plan in a target
        namespace, and a cutover time after which the source VM is stopped.

    Test plan:
        1. Prepare the disposable powered-on VM, wait until it appears in
           Forklift inventory, and create the copy-offload storage Secret
           with the storage-vendor credentials.
        2. Create a StorageMap that maps the disk to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VM's network. Confirm both are created.
        3. Create a warm Plan for the VM with copy-offload enabled. Check the
           Plan's Ready condition is True.
        4. Run the warm migration with a cutover time: the incremental
           copies run while the VM is on, then the migration finalizes at
           cutover. Check it reaches Succeeded.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for the disk.
        6. Validate the migrated VM: power state, connectivity, and CPU,
           memory, network, and storage equivalence with the source. After
           the checks, delete the migrated VM; the secret, maps, and Plan
           are removed with the run.

    Expected result:
        The warm migration completes through cutover with XCOPY used, and
        the migrated VM matches the source. On failure, inspect the
        migration CR status for the failed stage (incremental copy or
        cutover), the populate pod logs for the xcopyUsed value, and the
        Plan conditions.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        LOGGER.info("Starting copy-offload warm migration test")
        LOGGER.info("Datastore: %s, Storage vendor: %s", datastore_id, storage_vendor_product)

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute warm migration with cutover."""
        LOGGER.info("Executing warm migration with copy-offload acceleration")
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.copyoffload_sanity
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_scale_migration"])],
    indirect=True,
    ids=["MTV-572:copyoffload-scale"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadScaleMigration:
    """Copy-offload five VMs in one Plan and verify all migrations complete via XCOPY.

    Purpose/Regression:
        Copy-offload must scale beyond a single VM: a Plan containing five
        VMs, each with two disks, must migrate every VM via
        hardware-assisted copy with all VMs and disks validated. There is no
        induced failure; the scenario guards the controller's handling of
        many concurrent populate pods.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift, support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), and have capacity for five VMs with two
        disks each, with storage-vendor credentials available for the
        copy-offload storage Secret. Prepare a destination storage class
        that provisions block volumes (volumeMode: Block) and a reachable
        destination network. Use five disposable Linux VMs (e.g. cloned from
        a template) each with its template disk plus one added thick
        lazy-zeroed data disk, with guest-agent access. Have permission to
        create secrets, maps, and a Plan in a target namespace.

    Test plan:
        1. Prepare the five disposable VMs, wait until they appear in
           Forklift inventory, and create the copy-offload storage Secret
           with the storage-vendor credentials.
        2. Create a StorageMap that maps all VMs' disks to the block-volume
           storage class and carries the vsphereXcopyConfig offload plugin.
           Create a NetworkMap for the VMs' networks. Confirm both are
           created.
        3. Create a cold Plan covering all five VMs with copy-offload
           enabled. Check the Plan's Ready condition is True.
        4. Run the migration and check it reaches Succeeded for all five
           VMs.
        5. Verify XCOPY acceleration was used: the populate pod logs and the
           Plan task annotations report xcopyUsed for every disk of every
           VM.
        6. Validate all five migrated VMs: power state, connectivity, and
           CPU, memory, network, and storage equivalence with the sources,
           with disk counts matching. After the checks, delete the migrated
           VMs; the secret, maps, and Plan are removed with the run.

    Expected result:
        All five VMs migrate successfully with XCOPY used for every disk and
        each migrated VM matches its source. On failure, inspect the
        migration CR status for the failed VM and step, the populate pod
        logs for the xcopyUsed value, and the ForkliftController's
        concurrency limits.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        source_provider_data,
        copyoffload_storage_secret,
    ):
        """Create StorageMap with copy-offload configuration."""
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource."""
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        target_namespace,
        source_provider_inventory,
    ):
        """Create MTV Plan CR resource."""
        for vm in prepared_plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_copyoffload_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan,
        source_provider,
        destination_provider,
        source_provider_data,
        target_namespace,
        source_vms_namespace,
        source_provider_inventory,
        vm_ssh_connections,
    ):
        """Validate migrated VMs and verify disk count."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_simultaneous_copyoffload_migrations"])],
    indirect=True,
    ids=["MTV-574:simultaneous-copyoffload"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready_both_plans", "copyoffload_config", "copyoffload_ssh_key", "cleanup_migrated_vms"
)
class TestSimultaneousCopyoffloadMigrations:
    """Two copy-offload Plans must execute their migrations at the same time and both succeed.

    Purpose/Regression:
        Starting migrations from two separate copy-offload Plans must result
        in genuinely concurrent execution — both migrations in the Executing
        state at the same time — with both completing successfully via XCOPY.
        The failure path guarded against is serialization or interference
        between concurrent copy-offload migrations. There is no induced
        failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift, support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), and have capacity for two VMs, with
        storage-vendor credentials available for the copy-offload storage
        Secrets. Prepare a destination storage class that provisions block
        volumes (volumeMode: Block) and a reachable destination network. Use
        two disposable Linux VMs (e.g. cloned from a template) with
        thin-provisioned disks and guest-agent access, one per Plan. Have
        permission to create secrets, maps, and Plans in a target namespace.

    Test plan:
        1. Prepare the two disposable VMs, wait until they appear in
           Forklift inventory, and create a copy-offload storage Secret for
           each Plan with the storage-vendor credentials.
        2. For each Plan, create a StorageMap that maps its VM's disk to the
           block-volume storage class with the vsphereXcopyConfig offload
           plugin, and a NetworkMap for the VM's network. Confirm all four
           maps are created.
        3. Create a cold copy-offload Plan for each VM. Check both Plans'
           Ready conditions are True.
        4. Start both migrations and poll both Plans' migration statuses
           until both are in the Executing state at the same time (a
           migration completing before the other starts executing fails the
           check). Then wait for both to complete and check both reach
           Succeeded.
        5. Verify XCOPY acceleration was used for both migrations: each
           Plan's populate pod logs and task annotations report xcopyUsed
           for its disk.
        6. Validate both migrated VMs: power state, connectivity, resource
           equivalence, and disk count. After the checks, delete the migrated
           VMs; the secrets, maps, and Plans are removed with the run.

    Expected result:
        Both migrations are observed executing simultaneously and both
        succeed with XCOPY used, and both migrated VMs validate. On failure,
        inspect both migration CRs' statuses and timing, the populate pod
        logs for both migrations, and the ForkliftController's concurrency
        limits.
    """

    storage_map_1: StorageMap
    network_map_1: NetworkMap
    plan_resource_1: Plan

    storage_map_2: StorageMap
    network_map_2: NetworkMap
    plan_resource_2: Plan

    def test_create_storagemap_plan1(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration for first plan.

        Args:
            prepared_plan_1: Prepared plan configuration for plan 1
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for resources
            source_provider_data: Source provider configuration data
            copyoffload_storage_secret: Secret for copy-offload storage access

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails
        """
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan_1["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map_1 = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map_1, "StorageMap creation failed for plan 1"

    def test_create_storagemap_plan2(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration for second plan.

        Args:
            prepared_plan_2: Prepared plan configuration for plan 2
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for resources
            source_provider_data: Source provider configuration data
            copyoffload_storage_secret: Secret for copy-offload storage access

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails
        """
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan_2["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map_2 = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map_2, "StorageMap creation failed for plan 2"

    def test_create_networkmap_plan1(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for first plan.

        Args:
            prepared_plan_1: Prepared plan configuration for plan 1
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for resources
            multus_network_name: Network mapping from source to destination networks

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails
        """
        vms_names = [vm["name"] for vm in prepared_plan_1["virtual_machines"]]

        self.__class__.network_map_1 = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms_names,
        )
        assert self.network_map_1, "NetworkMap creation failed for plan 1"

    def test_create_networkmap_plan2(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for second plan.

        Args:
            prepared_plan_2: Prepared plan configuration for plan 2
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for resources
            multus_network_name: Network mapping from source to destination networks

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails
        """
        vms_names = [vm["name"] for vm in prepared_plan_2["virtual_machines"]]

        self.__class__.network_map_2 = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms_names,
        )
        assert self.network_map_2, "NetworkMap creation failed for plan 2"

    def test_create_plan1(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create first MTV Plan CR resource for copy-offload.

        Args:
            prepared_plan_1: Prepared plan configuration for plan 1
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for resources
            source_provider_inventory: Source provider inventory

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails
        """
        for vm in prepared_plan_1["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource_1 = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map_1,
            network_map=self.network_map_1,
            virtual_machines_list=prepared_plan_1["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan_1.get("warm_migration", False),
            copyoffload=prepared_plan_1.get("copyoffload", False),
            test_name="simultaneous-copyoffload-plan1",
        )
        assert self.plan_resource_1, "Plan creation failed for plan 1"

    def test_create_plan2(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create second MTV Plan CR resource for copy-offload.

        Args:
            prepared_plan_2: Prepared plan configuration for plan 2
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for resources
            source_provider_inventory: Source provider inventory

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails
        """
        for vm in prepared_plan_2["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_resource_2 = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map_2,
            network_map=self.network_map_2,
            virtual_machines_list=prepared_plan_2["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan_2.get("warm_migration", False),
            copyoffload=prepared_plan_2.get("copyoffload", False),
            test_name="simultaneous-copyoffload-plan2",
        )
        assert self.plan_resource_2, "Plan creation failed for plan 2"

    def test_migrate_vms_simultaneously(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute both copyoffload migrations simultaneously.

        Args:
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            target_namespace: Target namespace for migrations

        Returns:
            None

        Raises:
            AssertionError: If migration execution or completion fails or if simultaneous execution is not validated
        """
        LOGGER.info("Starting simultaneous execution of both copyoffload migration plans")

        # Create Migration CR for first plan
        migration_1 = create_and_store_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            resource=Migration,
            namespace=target_namespace,
            plan_name=self.plan_resource_1.name,
            plan_namespace=self.plan_resource_1.namespace,
            test_name="simultaneous-copyoffload-migration1",
        )
        LOGGER.info(f"Created Migration CR for plan 1: {migration_1.name}")

        # Create Migration CR for second plan
        migration_2 = create_and_store_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            resource=Migration,
            namespace=target_namespace,
            plan_name=self.plan_resource_2.name,
            plan_namespace=self.plan_resource_2.namespace,
            test_name="simultaneous-copyoffload-migration2",
        )
        LOGGER.info(f"Created Migration CR for plan 2: {migration_2.name}")

        wait_for_copyoffload_plan_secret(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource_1,
            namespace=target_namespace,
        )
        wait_for_copyoffload_plan_secret(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource_2,
            namespace=target_namespace,
        )

        # Create log capture callbacks before migrations can finish
        callback_1 = create_log_capture_callback(
            ocp_admin_client=ocp_admin_client,
            namespace=target_namespace,
            plan=self.plan_resource_1,
            fixture_store=fixture_store,
        )
        callback_2 = create_log_capture_callback(
            ocp_admin_client=ocp_admin_client,
            namespace=target_namespace,
            plan=self.plan_resource_2,
            fixture_store=fixture_store,
        )

        # Validate both migrations are executing simultaneously before either completes
        # Pass callbacks to ensure log capture starts as soon as EXECUTING is observed
        wait_for_concurrent_migration_execution(
            [self.plan_resource_1, self.plan_resource_2],
            callbacks={
                self.plan_resource_1.name: callback_1,
                self.plan_resource_2.name: callback_2,
            },
        )

        # Wait for both migrations to complete - poll both plans together
        wait_for_dual_migration_completion(
            plan_1=self.plan_resource_1,
            plan_2=self.plan_resource_2,
            callback_1=callback_1,
            callback_2=callback_2,
        )

    def test_check_xcopy_used_plan1(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for plan 1.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource_1,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms_plan1(
        self,
        prepared_plan_1: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs from first plan.

        Args:
            prepared_plan_1: Prepared plan configuration for plan 1
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_data: Source provider configuration data
            target_namespace: Target namespace where VMs were migrated
            source_vms_namespace: Source VMs namespace
            source_provider_inventory: Source provider inventory
            vm_ssh_connections: SSH connections manager for VMs

        Returns:
            None

        Raises:
            AssertionError: If VM validation fails or disk counts mismatch
        """
        check_vms(
            plan=prepared_plan_1,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map_1,
            storage_map_resource=self.storage_map_1,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan_1, target_namespace=target_namespace
        )

    def test_check_xcopy_used_plan2(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for plan 2.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource_2,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms_plan2(
        self,
        prepared_plan_2: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs from second plan.

        Args:
            prepared_plan_2: Prepared plan configuration for plan 2
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_data: Source provider configuration data
            target_namespace: Target namespace where VMs were migrated
            source_vms_namespace: Source VMs namespace
            source_provider_inventory: Source provider inventory
            vm_ssh_connections: SSH connections manager for VMs

        Returns:
            None

        Raises:
            AssertionError: If VM validation fails or disk counts mismatch
        """
        check_vms(
            plan=prepared_plan_2,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map_2,
            storage_map_resource=self.storage_map_2,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan_2, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_concurrent_xcopy_vddk_migration"])],
    indirect=True,
    ids=["MTV-569:concurrent-xcopy-vddk"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready_both_plans", "copyoffload_config", "copyoffload_ssh_key", "cleanup_migrated_vms"
)
class TestCopyoffloadConcurrentXcopyVddkMigration:
    """An XCOPY copy-offload Plan and a standard VDDK Plan must run concurrently and both succeed.

    Purpose/Regression:
        A copy-offload (XCOPY) migration and a standard (VDDK) migration
        started at the same time must not interfere with each other: both
        must be executing concurrently and complete successfully, each using
        its own transfer method. The failure path guarded against is the
        copy-offload path breaking the standard path (or vice versa) when
        both run together. There is no induced failure.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift and support hardware-assisted copy (VAAI XCOPY, e.g. NetApp
        ONTAP or Hitachi Vantara), with storage-vendor credentials available
        for the copy-offload storage Secret. Prepare a destination storage
        class that provisions block volumes (volumeMode: Block) and a
        reachable destination network. Use two disposable Linux VMs (e.g.
        cloned from a template) with thick lazy-zeroed disks and guest-agent
        access, one for each Plan. Have permission to create secrets, maps,
        and Plans in a target namespace.

    Test plan:
        1. Prepare the two disposable VMs, wait until they appear in
           Forklift inventory, and create the copy-offload storage Secret
           with the storage-vendor credentials.
        2. For the XCOPY Plan, create a StorageMap with the vsphereXcopyConfig
           offload plugin mapping the VM's disk to the block-volume storage
           class, and confirm the map carries the offload plugin. For the
           VDDK Plan, create a StorageMap for the other VM without the
           offload plugin, and confirm it has none. Create a NetworkMap per
           Plan and confirm all are created.
        3. Create a cold copy-offload Plan for one VM and a cold standard
           Plan for the other. Check both Plans' Ready conditions are True.
        4. Start both migrations and poll both Plans' statuses until both are
           in the Executing state at the same time. Then wait for both to
           complete and check both reach Succeeded.
        5. Verify XCOPY acceleration was used only by the copy-offload Plan:
           its populate pod logs and task annotations report xcopyUsed for
           its disk.
        6. Validate both migrated VMs: power state, connectivity, resource
           equivalence, and disk count. After the checks, delete the migrated
           VMs; the secret, maps, and Plans are removed with the run.

    Expected result:
        Both migrations execute concurrently and succeed; the copy-offload
        migration used XCOPY while the standard migration completed by its
        own transfer method, and both VMs validate. On failure, inspect both
        migration CRs' statuses and timing, the populate pod logs, and the
        StorageMaps' offload plugin configuration.
    """

    storage_map_xcopy: StorageMap
    network_map_xcopy: NetworkMap
    plan_xcopy: Plan

    storage_map_vddk: StorageMap
    network_map_vddk: NetworkMap
    plan_vddk: Plan

    def test_create_storagemap_xcopy(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration for XCOPY plan.

        Args:
            prepared_plan_1 (dict[str, Any]): Prepared plan configuration for the first VM.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace.
            source_provider_data (dict[str, Any]): Source provider data.
            copyoffload_storage_secret (Secret): Secret for copy-offload.

        Raises:
            AssertionError: If StorageMap creation fails or offloadPlugin is missing.
        """
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]

        vms_names = [vm["name"] for vm in prepared_plan_1["virtual_machines"]]

        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }

        self.__class__.storage_map_xcopy = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map_xcopy, "StorageMap creation failed for XCOPY plan"
        assert self.storage_map_xcopy.instance.spec.map[0].get("offloadPlugin"), (
            "XCOPY StorageMap missing offloadPlugin configuration"
        )

    def test_create_storagemap_vddk(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
    ) -> None:
        """Create standard StorageMap for VDDK plan.

        Args:
            prepared_plan_2 (dict[str, Any]): Prepared plan configuration for the second VM.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace.

        Raises:
            AssertionError: If StorageMap creation fails or offloadPlugin is present.
        """
        vms_names = [vm["name"] for vm in prepared_plan_2["virtual_machines"]]

        self.__class__.storage_map_vddk = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
        )
        assert self.storage_map_vddk, "StorageMap creation failed for VDDK plan"
        assert not self.storage_map_vddk.instance.spec.map[0].get("offloadPlugin"), (
            "VDDK StorageMap should NOT have offloadPlugin configuration"
        )

    def test_create_networkmap_xcopy(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for XCOPY plan.

        Args:
            prepared_plan_1 (dict[str, Any]): Prepared plan configuration for the first VM.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace.
            multus_network_name (dict[str, str]): Multus network name mapping.

        Raises:
            AssertionError: If NetworkMap creation fails.
        """
        vms_names = [vm["name"] for vm in prepared_plan_1["virtual_machines"]]

        self.__class__.network_map_xcopy = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms_names,
        )
        assert self.network_map_xcopy, "NetworkMap creation failed for XCOPY plan"

    def test_create_networkmap_vddk(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for VDDK plan.

        Args:
            prepared_plan_2 (dict[str, Any]): Prepared plan configuration for the second VM.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace.
            multus_network_name (dict[str, str]): Multus network name mapping.

        Raises:
            AssertionError: If NetworkMap creation fails.
        """
        vms_names = [vm["name"] for vm in prepared_plan_2["virtual_machines"]]

        self.__class__.network_map_vddk = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms_names,
        )
        assert self.network_map_vddk, "NetworkMap creation failed for VDDK plan"

    def test_create_plan_xcopy(
        self,
        prepared_plan_1: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource for XCOPY migration.

        Args:
            prepared_plan_1 (dict[str, Any]): Prepared plan configuration for the first VM.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            target_namespace (str): Target namespace.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Raises:
            AssertionError: If Plan creation fails.
        """
        for vm in prepared_plan_1["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_xcopy = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map_xcopy,
            network_map=self.network_map_xcopy,
            virtual_machines_list=prepared_plan_1["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan_1.get("warm_migration", False),
            copyoffload=True,
            test_name="concurrent-xcopy-plan",
        )
        assert self.plan_xcopy, "Plan creation failed for XCOPY plan"

    def test_create_plan_vddk(
        self,
        prepared_plan_2: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource for VDDK migration.

        Args:
            prepared_plan_2 (dict[str, Any]): Prepared plan configuration for the second VM.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            target_namespace (str): Target namespace.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Raises:
            AssertionError: If Plan creation fails.
        """
        for vm in prepared_plan_2["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]

        self.__class__.plan_vddk = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map_vddk,
            network_map=self.network_map_vddk,
            virtual_machines_list=prepared_plan_2["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan_2.get("warm_migration", False),
            copyoffload=False,
            test_name="concurrent-vddk-plan",
        )
        assert self.plan_vddk, "Plan creation failed for VDDK plan"

    def test_migrate_vms_concurrently(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute both migrations concurrently and verify simultaneous execution.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace.
        """
        LOGGER.info("Starting simultaneous execution of XCOPY and VDDK migration plans")

        # Create Migration CR for XCOPY plan
        migration_xcopy = create_and_store_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            resource=Migration,
            namespace=target_namespace,
            plan_name=self.plan_xcopy.name,
            plan_namespace=self.plan_xcopy.namespace,
            test_name="concurrent-xcopy-migration",
        )
        LOGGER.info(f"Created Migration CR for XCOPY plan: {migration_xcopy.name}")

        # Create Migration CR for VDDK plan
        migration_vddk = create_and_store_resource(
            client=ocp_admin_client,
            fixture_store=fixture_store,
            resource=Migration,
            namespace=target_namespace,
            plan_name=self.plan_vddk.name,
            plan_namespace=self.plan_vddk.namespace,
            test_name="concurrent-vddk-migration",
        )
        LOGGER.info(f"Created Migration CR for VDDK plan: {migration_vddk.name}")

        wait_for_copyoffload_plan_secret(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_xcopy,
            namespace=target_namespace,
        )

        # Create log capture callback before migration can finish
        callback_xcopy = create_log_capture_callback(
            ocp_admin_client=ocp_admin_client,
            namespace=target_namespace,
            plan=self.plan_xcopy,
            fixture_store=fixture_store,
        )

        # Validate both migrations are executing simultaneously before either completes
        # Pass callback to ensure XCOPY log capture starts as soon as EXECUTING is observed
        wait_for_concurrent_migration_execution(
            [self.plan_xcopy, self.plan_vddk],
            callbacks={self.plan_xcopy.name: callback_xcopy},
        )

        # Wait for both migrations to complete
        LOGGER.info("Waiting for XCOPY migration to complete")
        wait_for_migration_complate(plan=self.plan_xcopy, on_status_poll=callback_xcopy)
        LOGGER.info("XCOPY migration completed")

        LOGGER.info("Waiting for VDDK migration to complete")
        wait_for_migration_complate(plan=self.plan_vddk)
        LOGGER.info("VDDK migration completed")

    def test_check_xcopy_used(
        self, ocp_admin_client: DynamicClient, target_namespace: str, fixture_store: dict[str, Any]
    ) -> None:
        """Verify XCOPY acceleration was used for XCOPY plan.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_xcopy,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms_xcopy(
        self,
        prepared_plan_1: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs from XCOPY plan.

        Args:
            prepared_plan_1 (dict[str, Any]): Prepared plan configuration for the first VM.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_data (dict[str, Any]): Source provider data.
            target_namespace (str): Target namespace.
            source_vms_namespace (str): Source VMs namespace.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (SSHConnectionManager | None): SSH connection manager.
        """
        check_vms(
            plan=prepared_plan_1,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map_xcopy,
            storage_map_resource=self.storage_map_xcopy,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan_1, target_namespace=target_namespace
        )

    def test_check_vms_vddk(
        self,
        prepared_plan_2: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs from VDDK plan.

        Args:
            prepared_plan_2 (dict[str, Any]): Prepared plan configuration for the second VM.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_data (dict[str, Any]): Source provider data.
            target_namespace (str): Target namespace.
            source_vms_namespace (str): Source VMs namespace.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (SSHConnectionManager | None): SSH connection manager.
        """
        check_vms(
            plan=prepared_plan_2,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map_vddk,
            storage_map_resource=self.storage_map_vddk,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_vm_disk_count(
            destination_provider=destination_provider, plan=prepared_plan_2, target_namespace=target_namespace
        )


@pytest.mark.vsphere
@pytest.mark.copyoffload
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_copyoffload_vm_populator_throttling_migration"])],
    indirect=True,
    ids=["MTV-777:copyoffload-vm-populator-throttling"],
)
@pytest.mark.usefixtures(
    "vmware_cloud_init_ready",
    "multus_network_name",
    "copyoffload_config",
    "vm_populator_inflight_forkliftcontroller",
    "copyoffload_ssh_key",
    "cleanup_migrated_vms",
)
class TestCopyoffloadVmPopulatorThrottlingMigration:
    """Combined VM and populate pod throttling: two VMs with multiple disks share per-host limits on one ESXi host (MTV-777).

    Purpose/Regression:
        With both the per-host VM in-flight limit (1) and the per-host
        populator limit (2) set low, migrating two VMs with three disks each
        from the same ESXi host must respect both limits: at most one VM
        transferring per host at a time and at most two populate pods per
        host at a time, with PopulatorThrottled events on the queued PVCs —
        counted per VM, not globally (MTV-777). The migration must still
        complete with XCOPY.

    Prerequisites:
        Register a vSphere source provider and an OpenShift destination with
        MTV installed and the copy-offload feature enabled on the
        ForkliftController. Storage must be shared between vSphere and
        OpenShift, support hardware-assisted copy (VAAI XCOPY), and have
        capacity for two VMs with three disks each, with storage-vendor
        credentials available for the copy-offload storage Secret. Prepare a
        destination storage class that provisions block volumes
        (volumeMode: Block) and a reachable destination network. Use two
        disposable Linux VMs (e.g. cloned from a template) each with its
        template disk plus two added data disks, placed on the same ESXi
        host, with guest-agent access. Have permission to patch the
        ForkliftController (to set and later restore both limits), create
        secrets, maps, and a Plan in a target namespace, and prevent DRS from
        moving the VMs to other hosts during the scenario.

    Test plan:
        1. Set the ForkliftController's per-host VM in-flight limit to 1 and
           per-host populator limit to 2, recording the previous values.
           Prepare the two VMs on the same ESXi host, wait until they appear
           in Forklift inventory, and create the copy-offload storage Secret.
        2. Create a StorageMap that maps all disks to the block-volume
           storage class with the vsphereXcopyConfig offload plugin, and a
           NetworkMap for the VMs' networks. Confirm both are created.
        3. Create a cold Plan covering both VMs with copy-offload enabled.
           Check the Plan's Ready condition is True.
        4. Run the migration while monitoring VM in-flight and populate pod
           counts per host, and check it reaches Succeeded.
        5. Verify throttling from the monitoring samples: the peak number of
           concurrently transferring VMs per host never exceeds 1; the peak
           number of concurrently active populate pods per host never exceeds
           2; populate pods carry source host labels; and the number of PVCs
           with PopulatorThrottled events is at least the per-VM queued-disk
           total (VM count times disks beyond the populator limit).
        6. Verify XCOPY was used for every disk, then validate both migrated
           VMs: power state, connectivity, resource equivalence, and disk
           count. After the checks, delete the migrated VMs, restore the
           ForkliftController limits, and remove the secret, maps, and Plan.

    Expected result:
        The migration completes with both limits observed: peak per-host VM
        concurrency of 1, peak per-host populate pod concurrency of 2, and
        PopulatorThrottled events covering the queued disks. On failure,
        inspect the PopulatorThrottled events on the PVCs, the populate pod
        labels and logs, the ForkliftController's configured limits, and the
        migration CR status.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
    max_concurrent_vms_by_host: dict[str, int]
    max_concurrent_populators_by_host: dict[str, int]

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        source_provider_data: dict[str, Any],
        copyoffload_storage_secret: Secret,
    ) -> None:
        """Create StorageMap with copy-offload configuration.

        Args:
            prepared_plan (dict[str, Any]): Prepared plan configuration with VM names.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace for the StorageMap.
            source_provider_data (dict[str, Any]): Source provider configuration data.
            copyoffload_storage_secret (Secret): Copy-offload storage credentials secret.
        """
        copyoffload_config_data = source_provider_data["copyoffload"]
        storage_vendor_product = copyoffload_config_data["storage_vendor_product"]
        datastore_id = copyoffload_config_data["datastore_id"]
        storage_class = py_config["storage_class"]
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        offload_plugin_config = {
            "vsphereXcopyConfig": {
                "secretRef": copyoffload_storage_secret.name,
                "storageVendorProduct": storage_vendor_product,
            }
        }
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            target_namespace=target_namespace,
            source_provider=source_provider,
            destination_provider=destination_provider,
            ocp_admin_client=ocp_admin_client,
            source_provider_inventory=source_provider_inventory,
            vms=vms_names,
            storage_class=storage_class,
            datastore_id=datastore_id,
            offload_plugin_config=offload_plugin_config,
            volume_mode="Block",
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource.

        Args:
            prepared_plan (dict[str, Any]): Prepared plan configuration with VM names.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace for the NetworkMap.
            multus_network_name (dict[str, str]): Multus network name mapping.
        """
        vms_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms_names,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource.

        Args:
            prepared_plan (dict[str, Any]): Prepared plan configuration with VM names.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            target_namespace (str): Target namespace for the Plan.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
        """
        for vm in prepared_plan["virtual_machines"]:
            vm_data = source_provider_inventory.get_vm(vm["name"])
            vm["id"] = vm_data["id"]
        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            copyoffload=prepared_plan.get("copyoffload", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Execute migration while monitoring VM and populator concurrency per ESXi host.

        Args:
            prepared_plan (dict[str, Any]): Prepared plan configuration with VM names.
            fixture_store (dict[str, Any]): Fixture store for resource tracking and log caching.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where the Migration CR is created.
            source_provider_inventory (ForkliftInventory): Inventory API for ESXi host lookup.
        """
        vm_names = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        vm_results, populator_results = execute_migration_monitoring_vm_and_populator_inflight(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_names=vm_names,
            max_vm_inflight=VM_INFLIGHT_LIMIT,
            max_populator_inflight=VM_POPULATOR_INFLIGHT_LIMIT,
        )
        self.__class__.max_concurrent_vms_by_host = vm_results
        self.__class__.max_concurrent_populators_by_host = populator_results

    def test_verify_vm_inflight_throttling(self, prepared_plan: dict[str, Any]) -> None:
        """Verify peak concurrent VMs per ESXi host respects VM_INFLIGHT_LIMIT.

        Args:
            prepared_plan (dict[str, Any]): Prepared plan configuration (for VM count).
        """
        verify_vm_inflight_throttling(
            max_concurrent_by_host=self.max_concurrent_vms_by_host,
            vm_count=len(prepared_plan["virtual_machines"]),
            max_vm_inflight=VM_INFLIGHT_LIMIT,
        )

    def test_verify_populator_throttling(
        self,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        fixture_store: dict[str, Any],
        prepared_plan: dict[str, Any],
    ) -> None:
        """Verify sourceHost labels, PopulatorThrottled events, and peak concurrency.

        With VM_INFLIGHT_LIMIT=1, VMs migrate sequentially, so the minimum expected
        PopulatorThrottled count is per-VM-batch
        (``vm_count * max(0, disks_per_vm - limit)``), not ``total_pods - limit``.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods and PVCs exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
            prepared_plan (dict[str, Any]): Prepared plan configuration (for VM/disk counts).
        """
        virtual_machines = prepared_plan["virtual_machines"]
        vm_count = len(virtual_machines)
        disks_per_vm = 1 + len(virtual_machines[0]["add_disks"])
        min_expected_throttled = vm_count * max(0, disks_per_vm - VM_POPULATOR_INFLIGHT_LIMIT)
        verify_populator_throttling(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            max_concurrent_by_host=self.max_concurrent_populators_by_host,
            fixture_store=fixture_store,
            max_populator_inflight=VM_POPULATOR_INFLIGHT_LIMIT,
            min_expected_throttled=min_expected_throttled,
        )

    def test_check_xcopy_used(
        self,
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        fixture_store: dict[str, Any],
    ) -> None:
        """Verify XCOPY acceleration was used for all disks.

        Args:
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Namespace where populate pods exist.
            fixture_store (dict[str, Any]): Fixture store containing cached populate pod logs.
        """
        verify_xcopy_used(
            ocp_admin_client=ocp_admin_client,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            expected_xcopy_used=True,
            fixture_store=fixture_store,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
    ) -> None:
        """Validate migrated VMs.

        Args:
            prepared_plan (dict[str, Any]): Prepared plan configuration.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_data (dict[str, Any]): Source provider configuration data.
            source_vms_namespace (str): Namespace of the source VMs.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (SSHConnectionManager | None): SSH connection manager.
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
