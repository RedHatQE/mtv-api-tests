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
from utilities.post_migration import check_vms, verify_excluded_disks
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_exclude_disks_cold_migration"],
        )
    ],
    indirect=True,
    ids=["exclude-disks-cold"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
class TestExcludeDisksColdMigration:
    """Cold migration of a vSphere VM with one disk excluded from the migration.

    Purpose/Regression:
        A migration Plan can list source disks to exclude by bus address (excludeDisks).
        Every excluded disk must be skipped: no destination volume is created for it and
        it is not attached to the migrated VM, while all other disks migrate normally and
        the VM boots. This scenario covers the individual-disk exclusion feature
        (MTV-6526) and guards against regressions where an excluded disk is still copied
        or attached, or a non-excluded disk is dropped.

    Prerequisites:
        - A registered vSphere source provider and an OpenShift Virtualization
          destination provider, with accessible destination storage and network
          mappings (any storage class that can create the migrated volumes).
        - A disposable Linux (RHEL 8) source VM with VMware Tools installed whose
          boot disk is the first disk on its SCSI controller (scsi0:0, 16 GB),
          followed by a single 4 GB data disk at scsi0:1. The clone gets one extra
          4 GB data disk attached at the next free unit (scsi0:2), so the kept
          added disk and the excluded native disk have equal capacity.
        - Power on the clone so the cold migration ends with a running destination
          VM, and confirm the provider inventory reports the clone before the Plan
          is created.
        - Migrated VMs must land in a dedicated namespace so their volumes can be
          counted without interference from other migrations.

    Test plan:
        1. Clone the source VM, attach one extra 4 GB data disk, and wait for the
           provider inventory to report the clone.
        2. Create a StorageMap and a NetworkMap for the clone's disks and networks, and
           confirm both resources are created.
        3. Create a cold migration Plan that lists the clone with excludeDisks set to
           the data disk's bus address (scsi0:1), and confirm the Plan reaches Ready —
           readiness proves the address exists on the VM (unknown addresses fail
           validation) and that not every disk is excluded.
        4. Run the migration and confirm it completes with status Succeeded.
        5. Before any cleanup, confirm on the destination: the migrated VM has exactly
           two disks attached — the boot disk (16 GB) and the added 4 GB disk — and its
           namespace holds exactly two PersistentVolumeClaims, so no volume was created
           for the excluded native 4 GB data disk. The kept and excluded 4 GB disks have
           the same capacity, so this step verifies disk identity by serial: each kept
           source disk's serial (vSphere disk UUID prefix) must appear on a destination
           disk, and no destination disk may carry the excluded disk's serial.
        6. Confirm the migrated VM boots, its guest agent reports, and its CPU, memory,
           network, and storage match the source VM and the mappings.
        7. Delete the migrated VM with its volumes, the Plan, the maps, and the cloned
           source VM.

    Expected result:
        The migration succeeds; the migrated VM runs with exactly the non-excluded
        disks attached (the boot disk and the added 4 GB disk), identified by disk
        serial provenance, and only their volumes exist in the dedicated namespace —
        the excluded native 4 GB data disk is neither copied nor attached. On failure,
        inspect the Plan conditions for validation errors (unknown excludeDisks bus
        address, every disk excluded), the Migration status for the failed pipeline
        step, and the VM volume list in the dedicated namespace for missing or
        leftover volumes.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],  # untyped pytest-testconfig JSON; accessed fields documented below
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Create StorageMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (OCPProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Returns:
            None.

        Raises:
            AssertionError: If StorageMap creation fails.
        """
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
        assert self.__class__.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],  # untyped pytest-testconfig JSON; accessed fields documented below
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (OCPProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            multus_network_name (dict[str, str]): Multus network name and namespace.

        Returns:
            None.

        Raises:
            AssertionError: If NetworkMap creation fails.
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            multus_network_name=multus_network_name,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.__class__.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],  # untyped pytest-testconfig JSON; accessed fields documented below
        fixture_store: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Create MTV Plan CR with the excluded disk bus addresses.

        The per-VM exclude_disks bus addresses are renamed to the Plan
        spec.vms[].excludeDisks field by this test before plan creation, keeping
        the shared builder feature-agnostic.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (OCPProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Returns:
            None.

        Raises:
            AssertionError: If Plan creation fails.
        """
        populate_vm_ids(prepared_plan, source_provider_inventory)

        # Exclusion-specific key rename: the shared plan builder is feature-agnostic and
        # passes VM dict keys through to spec.vms, so this test shapes its own spec fields.
        vms_for_plan = []
        for vm in prepared_plan["virtual_machines"]:
            spec_vm = dict(vm)
            if "exclude_disks" in spec_vm:
                spec_vm["excludeDisks"] = spec_vm.pop("exclude_disks")
            vms_for_plan.append(spec_vm)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.__class__.storage_map,
            network_map=self.__class__.network_map,
            virtual_machines_list=vms_for_plan,
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
            vm_target_namespace=prepared_plan["_vm_target_namespace"],
        )
        assert self.__class__.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute cold migration.

        Args:
            fixture_store (dict[str, Any]): Resource tracking dictionary.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration resources.

        Returns:
            None.

        Raises:
            MigrationPlanExecError: If migration execution fails.
        """
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.__class__.plan_resource,
            target_namespace=target_namespace,
        )

    def test_verify_excluded_disks(
        self,
        prepared_plan: dict[str, Any],  # untyped pytest-testconfig JSON; accessed fields documented below
        destination_provider: "OCPProvider",
        ocp_admin_client: "DynamicClient",
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Verify excluded disks are absent from the migrated VMs and their namespace.

        Verifies every migrated VM has the kept disks attached, matched by serial
        provenance with volume-mode-aware capacity checks, and that no PVC exists for
        the excluded disks in the VM namespace.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            destination_provider (OCPProvider): Destination provider connection.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Returns:
            None.

        Raises:
            AssertionError: If a migrated VM's disks or the namespace PVC set do not
                match the non-excluded source disks.
            ValueError: If an excluded bus address matches no source disk.
        """
        verify_excluded_disks(
            plan=prepared_plan,
            ocp_admin_client=ocp_admin_client,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],  # untyped pytest-testconfig JSON; accessed fields documented below
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager | None",
    ) -> None:
        """Validate migrated VMs.

        Args:
            prepared_plan (dict[str, Any]): Test plan configuration with VM details.
            source_provider (BaseProvider): Source provider connection.
            destination_provider (OCPProvider): Destination provider connection.
            source_provider_data (dict[str, Any]): Source provider configuration.
            source_vms_namespace (str): Source VMs namespace.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (SSHConnectionManager | None): SSH connections for
                connectivity testing.

        Returns:
            None.

        Raises:
            AssertionError: If any VM validation check fails.
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.__class__.network_map,
            storage_map_resource=self.__class__.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
