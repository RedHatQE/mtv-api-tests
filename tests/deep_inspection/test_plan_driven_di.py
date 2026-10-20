from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from exceptions.exceptions import MigrationPlanExecError
from utilities.deep_inspection import verify_di_concerns_block_migration
from utilities.migration_utils import get_cutover_value
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.warm
@pytest.mark.deep_inspection
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_warm_di_concerns"],
        )
    ],
    indirect=True,
    ids=["warm-di-concerns-block"],
)
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestWarmDIConcernsBlockMigration:
    """Critical Deep Inspection concerns must block a warm migration at the preflight inspection step.

    Purpose/Regression:
        When pre-migration Deep Inspection of a warm Plan's VMs reports
        Critical concerns, the migration must not proceed: it must fail at
        the PreflightInspection step with an error mentioning critical
        concerns. The failure path is induced by the VM's guest
        configuration: an /etc/fstab entry referencing a device by
        /dev/disk/by-path/, which Deep Inspection flags as Critical. No VM
        is migrated.

    Prerequisites:
        Register and connect a vSphere source provider and an OpenShift
        destination with MTV installed and Deep Inspection enabled for warm
        migrations, with accessible destination storage and network. Use a
        disposable, powered-on Linux VM whose /etc/fstab contains a
        /dev/disk/by-path/ device reference (this raises the Critical
        concern), and guest access to prepare it. Have permission to create
        maps and a Plan in a target namespace and to adjust the controller's
        pre-copy interval if needed.

    Test plan:
        1. Prepare the disposable powered-on VM with the by-path fstab entry
           and wait until it appears in Forklift inventory.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network. Confirm both are created.
        3. Create a warm Plan for the VM. Check the Plan's Ready condition
           is True.
        4. Run the warm migration with a cutover and check that it fails at
           the PreflightInspection step for the VM, with the step's error
           citing critical concerns.
        5. Confirm on the failed migration's status that the VM's
           PreflightInspection pipeline step carries the critical-concerns
           error before any migration resources are cleaned up. The maps and
           Plan are then removed with the run; no destination VM is created.

    Expected result:
        The migration is blocked at PreflightInspection with a
        critical-concerns error for the VM, and no VM is migrated. On
        failure, inspect the migration CR status (the VM's pipeline steps
        and their errors), the Plan-created DeepInspection Conversion CRs'
        inspection results for the Critical concern, and the inspection pod
        logs.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource for migration."""
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
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for migration."""
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
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Create warm migration plan for VM with Critical concerns."""
        populate_vm_ids(plan=prepared_plan, inventory=source_provider_inventory)
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
        )
        assert self.plan_resource, "Plan creation failed"

    @pytest.mark.usefixtures("class_plan_config")
    def test_migrate_vms_fails(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute migration and verify it fails due to Critical DI concerns."""
        with pytest.raises(MigrationPlanExecError, match="PreflightInspection"):
            execute_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.plan_resource,
                target_namespace=target_namespace,
                cut_over=get_cutover_value(),
            )

    def test_verify_di_concerns(
        self,
        prepared_plan: dict[str, Any],
    ) -> None:
        """Verify Critical concerns blocked migration at DI step."""
        expected_vm_names = {vm["name"] for vm in prepared_plan["virtual_machines"]}
        verify_di_concerns_block_migration(
            plan_resource=self.plan_resource,
            expected_vm_names=expected_vm_names,
        )
