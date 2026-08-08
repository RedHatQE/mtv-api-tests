"""
AAP hook integration test — migration with AWX PreHook and PostHook.

Validates that Hook CRs with ``spec.aap.jobTemplateId`` correctly trigger
AWX job templates during migration. The test deploys AWX, creates job
templates from the mtv-aap-test-playbooks repo, configures MTV with AAP
settings, and runs a cold migration with both PreHook and PostHook.
A successful migration confirms Forklift correctly launched the AWX jobs
and waited for their completion before proceeding through the pipeline.
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
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.ocp_provider import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.aap
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_aap_hook_migration"])],
    indirect=True,
    ids=["aap-hook-migration"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms", "aap_hook_refs")
class TestAapHookMigration:
    """Test AAP hook integration — cold migration with AWX PreHook and PostHook.

    Follows the standard 5-step migration pattern. The AAP hooks are created
    by the ``aap_hook_refs`` fixture which injects Hook CR references into
    ``prepared_plan``. A successful migration confirms PreHook ran before
    disk transfer and PostHook ran after VM creation.
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
        """Create StorageMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace for migration.
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
        assert self.storage_map

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
        """Create NetworkMap resource for migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            target_namespace (str): Target namespace for migration.
            multus_network_name (dict[str, str]): Name of the multus network.
        """
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
        assert self.network_map

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
        """Create MTV Plan CR with AAP PreHook and PostHook.

        The hook references (``_pre_hook_name``, ``_post_hook_name``, etc.)
        are injected into ``prepared_plan`` by the ``aap_hook_refs`` fixture.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            target_namespace (str): Target namespace for migration.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
        """
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
            warm_migration=prepared_plan.get("warm_migration", False),
            target_power_state=prepared_plan.get("target_power_state"),
            pre_hook_name=prepared_plan["_pre_hook_name"],
            pre_hook_namespace=prepared_plan["_pre_hook_namespace"],
            after_hook_name=prepared_plan["_post_hook_name"],
            after_hook_namespace=prepared_plan["_post_hook_namespace"],
        )
        assert self.plan_resource

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute migration — both AAP hooks should succeed.

        A successful migration confirms the full AAP hook pipeline:
        Initialize → PreHook (AWX job) → DiskAllocation → ImageConversion →
        DiskTransfer → VirtualMachineCreation → PostHook (AWX job) → Completed.

        Args:
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration.
        """
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        target_namespace: str,
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager | None",
    ) -> None:
        """Validate migrated VMs post-migration.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_data (dict[str, Any]): Source provider configuration data.
            target_namespace (str): Target namespace for migration.
            source_vms_namespace (str): Namespace of source VMs.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (SSHConnectionManager | None): SSH connections to migrated VMs.
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
