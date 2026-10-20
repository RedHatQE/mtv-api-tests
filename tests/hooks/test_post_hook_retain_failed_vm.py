from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from exceptions.exceptions import MigrationPlanExecError
from utilities.hooks import validate_hook_failure_and_check_vms
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
    from libs.ocp_provider import OCPProvider
    from libs.forklift_inventory import ForkliftInventory
    from utilities.ssh_utils import SSHConnectionManager

    from libs.base_provider import BaseProvider


@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.openstack
@pytest.mark.openshift
@pytest.mark.tier0
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_post_hook_retain_failed_vm"])],
    indirect=True,
    ids=["post-hook-retain-failed-vm"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestPostHookRetainFailedVm:
    """A migration that fails at PostHook must retain the already-created destination VM.

    Purpose/Regression:
        When a PostHook fails, the destination VM has already been created by
        earlier pipeline stages and must be retained for inspection instead
        of being deleted with the failed migration. The chosen failure path
        is an intentionally failing post-migration hook: the PreHook
        succeeds, disk transfer and VM creation succeed, and only the
        PostHook fails, so the retained VM should still exist and validate.

    Prerequisites:
        Register and connect a source provider and an OpenShift destination
        with MTV installed. Prepare accessible destination storage and
        network. Use a disposable, powered-on Linux VM with guest-agent access
        and a disk and network interface supported by both providers. Have
        permission to create maps, hooks, and a Plan in a target namespace,
        and a post-migration hook (e.g. an Ansible playbook) that is
        guaranteed to fail. Use a cold migration with target power state off.

    Test plan:
        1. Prepare the disposable source VM, verify it is visible in
           Forklift inventory, and create a succeeding PreHook and a failing
           PostHook.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network. Confirm both are created.
        3. Create a cold Plan for the VM that attaches both hooks and sets
           target power state off. Check the Plan's Ready condition is True.
        4. Run the migration and check that it fails, that every VM in the
           migration failed at the same step, and that the failed step is
           PostHook.
        5. Confirm the destination VM was retained by the failed migration:
           it exists and passes the standard VM validation (power state and
           CPU, memory, network, and storage equivalence with the source).
           After the checks, delete the retained destination VM; the hooks,
           maps, and Plan are removed with the run.

    Expected result:
        The migration fails exactly at PostHook and the destination VM
        remains present and validates despite the failure. On failure,
        inspect the VM's failed step in the migration CR status (a step other
        than PostHook means the wrong failure path was exercised), the Hook
        CR conditions and hook pod logs for the failing playbook output, and
        the retained VM's name and state in the target namespace.
    """

    storage_map: StorageMap | None = None
    network_map: NetworkMap | None = None
    plan_resource: Plan | None = None
    should_check_vms: bool = False

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

        Returns:
            None

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

        Returns:
            None

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
        """Create MTV Plan CR resource with PreHook and PostHook.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            target_namespace (str): Target namespace for migration.
            source_provider_inventory (ForkliftInventory): Source provider inventory.

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails.
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
            warm_migration=prepared_plan["warm_migration"],
            target_power_state=prepared_plan["target_power_state"],
            pre_hook_name=prepared_plan["_pre_hook_name"],
            pre_hook_namespace=prepared_plan["_pre_hook_namespace"],
            after_hook_name=prepared_plan["_post_hook_name"],
            after_hook_namespace=prepared_plan["_post_hook_namespace"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute migration - PreHook succeeds but PostHook fails.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            fixture_store (dict[str, Any]): Fixture store for resource tracking.
            ocp_admin_client (DynamicClient): OpenShift admin client.
            target_namespace (str): Target namespace for migration.

        Returns:
            None

        Raises:
            MigrationPlanExecError: If migration fails or times out (expected for post-hook failure).
        """
        expected_result = prepared_plan["expected_migration_result"]

        if expected_result == "fail":
            with pytest.raises(MigrationPlanExecError) as exc_info:
                execute_migration(
                    ocp_admin_client=ocp_admin_client,
                    fixture_store=fixture_store,
                    plan=self.plan_resource,
                    target_namespace=target_namespace,
                )
            try:
                self.__class__.should_check_vms = validate_hook_failure_and_check_vms(self.plan_resource, prepared_plan)
            except Exception as e:
                # Chain with original migration error so the root cause is visible in traceback
                e.__cause__ = exc_info.value
                raise
        else:
            execute_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.plan_resource,
                target_namespace=target_namespace,
            )
            self.__class__.should_check_vms = True

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
        """Validate migrated VMs - PostHook fails after migration, so VMs should exist.

        Args:
            prepared_plan (dict[str, Any]): The prepared migration plan.
            source_provider (BaseProvider): Source provider instance.
            destination_provider (OCPProvider): Destination provider instance.
            source_provider_data (dict[str, Any]): Source provider configuration data.
            target_namespace (str): Target namespace for migration.
            source_vms_namespace (str): Namespace of source VMs.
            source_provider_inventory (ForkliftInventory): Source provider inventory.
            vm_ssh_connections (SSHConnectionManager | None): SSH connections to migrated VMs.

        Returns:
            None

        Raises:
            pytest.Failed: If VM validation checks fail.
        """
        # Runtime skip needed - decision based on previous test's migration execution result
        if not self.__class__.should_check_vms:
            pytest.skip("Skipping VM checks - hook failed before VM migration")

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
