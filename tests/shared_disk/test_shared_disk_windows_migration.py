"""Shared disk migration tests for Windows VMs.

Tests VM-level migrateSharedDisks overrides for Windows Server VMs in a single
migration plan. The owner VM (migrateSharedDisks=true) migrates the shared disk
PVC, while the consumer VM (migrateSharedDisks=false) skips it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms
from utilities.shared_disk import label_shared_disk_on_source_windows, verify_shared_disk_data_windows
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.vsphere
@pytest.mark.shared_disk
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_shared_disk_windows_migration"],
        )
    ],
    indirect=True,
    ids=["shared-disk-windows"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestSharedDiskWindowsMigration:
    """Cold-migrate two Windows VMs sharing one NTFS disk; the owner migrates the disk and both keep bidirectional access.

    Purpose/Regression:
        Windows variant of the shared-disk scenario: with VM-level
        migrateSharedDisks overrides in one Plan, the owner VM
        (migrateSharedDisks=true) must migrate the shared NTFS disk while
        the consumer VM (migrateSharedDisks=false) skips it, and both
        migrated Windows VMs must see the same shared disk with working
        bidirectional read/write. The shared disk is located after migration
        by an NTFS volume label set on the source before migrating, since
        Windows has no stable device paths. There is no induced failure.

    Prerequisites:
        Register and connect a vSphere source provider and an OpenShift
        destination with MTV installed and shared-disk support, with
        accessible destination storage and network. Use two disposable
        Windows VMs that share one additional NTFS-formatted disk, powered
        off before migration, with VMware guest operations credentials (to
        label the disk on the source) and remote access to the migrated
        guests. Have permission to create maps and a Plan in a target
        namespace, and use a disposable environment so the shared disk can
        be written to.

    Test plan:
        1. While the source VMs are reachable, set a unique NTFS volume
           label on the shared disk (matching the shared VMDK to the Windows
           disk by its serial number) and record it. Wait until both VMs
           appear in Forklift inventory.
        2. Create a StorageMap covering both VMs' disks and a NetworkMap
           for their networks. Confirm both are created.
        3. Create a cold Plan covering both VMs with the shared-disk
           setting enabled for the owner VM and disabled for the consumer
           VM, and target power state on. Check the Plan's Ready condition
           is True.
        4. Run the migration and check it reaches Succeeded for both VMs.
        5. Verify the shared disk from both migrated VMs, locating it by
           the recorded NTFS label: on VM1 bring the disk online and write
           data; on VM2 bring it online, refresh the disk cache, read VM1's
           data, and write new data; back on VM1 refresh the cache and read
           VM2's data. All reads must match what was written.
        6. Run the standard VM validation on both VMs: power state,
           connectivity, and CPU, memory, network, and storage equivalence
           with the sources. After the checks, delete both migrated VMs and
           the shared disk PVC; the maps and Plan are removed with the run.

    Expected result:
        Both Windows VMs migrate, the shared NTFS disk exists once on the
        destination with the expected volume label, and data written by
        either VM is readable by the other. On failure, inspect the
        migration CR status for each VM's failed step, the shared disk's PVC
        and the VM volume attachments on the destination, and the guest
        disk-online/read errors on both VMs.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_label_shared_disk(
        self,
        prepared_plan: dict[str, Any],  # Heterogeneous: VM specs, flags, runtime data
        source_provider: BaseProvider,
        source_provider_data: dict[str, Any],  # Provider config: credentials, endpoints, settings
        fixture_store: dict[str, Any],  # Session state: UUIDs, teardown tracking
    ) -> None:
        """Label the shared disk on source VM via VMware Guest Operations before migration."""
        label_shared_disk_on_source_windows(
            source_provider=source_provider,  # type: ignore[arg-type]  # vsphere marker guarantees VMWareProvider
            prepared_plan=prepared_plan,
            source_provider_data=source_provider_data,
            session_uuid=fixture_store["session_uuid"],
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
    ) -> None:
        """Create StorageMap resource for both VMs."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
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
        """Create NetworkMap resource for both VMs."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms,
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
        """Create MTV Plan CR with VM-level migrateSharedDisks overrides."""
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan["warm_migration"],
            migrate_shared_disks=prepared_plan["migrate_shared_disks"],
            target_power_state=prepared_plan["target_power_state"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute migration for both VMs in a single plan."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_verify_shared_disk_data(
        self,
        prepared_plan: dict[str, Any],
        vm_ssh_connections: SSHConnectionManager,
        source_provider_data: dict[str, Any],
        ocp_admin_client: DynamicClient,
    ) -> None:
        """Verify shared disk read/write access from both Windows VMs."""
        verify_shared_disk_data_windows(
            prepared_plan=prepared_plan,
            vm_ssh_connections=vm_ssh_connections,
            source_provider_data=source_provider_data,
            ocp_admin_client=ocp_admin_client,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager,
    ) -> None:
        """Validate both migrated VMs."""
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
