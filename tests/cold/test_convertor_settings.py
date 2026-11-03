from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.convertor_pod_placement import ConvertorPodObserver
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vm_instance_type, check_vms
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_convertor_settings_and_instance_type"])],
    indirect=True,
    ids=["convertor-settings-instance-type-cold"],
)
@pytest.mark.usefixtures("mtv_version_checker")
@pytest.mark.min_mtv_version("5.0.0")
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestConvertorSettingsAndInstanceType:
    """Cold-migrate a vSphere VM with a convertor node selector and a per-VM instanceType and verify both.

    Purpose/Regression:
        Positive coverage for MTV-7085 Plan settings: a Plan's convertorNodeSelector
        must be inherited by the virt-v2v convertor pod, which must run on the worker
        node whose built-in kubernetes.io/hostname label the selector pins, and a
        per-VM instanceType must make the migrated
        VM take its CPU and memory from the instancetype instead of the source VM.
        There is no induced failure; the convertor checks must observe the
        short-lived pod while the migration is running.

    Prerequisites:
        Register and connect a vSphere source provider and an OpenShift destination
        with MTV 5.0 or later installed (instanceType and convertorNodeSelector
        plan settings require MTV >= 5.0). Provide accessible destination storage and
        network, permission to create migration
        resources, and at least one Ready, schedulable worker that can run the
        convertor pod. The destination cluster must provide the standard
        cluster-scoped instancetype used by the Plan (e.g. u1.medium). Use a
        disposable, cloneable RHEL VM with VMware Tools and SSH access, powered on
        before migration so its guest agent reports, with CPU and memory values
        that differ from the chosen instancetype.

    Test plan:
        1. Prepare the disposable VM clone powered on and wait until it appears in
           Forklift inventory. Select the Ready, schedulable worker node with the
           most requestable memory (allocatable minus pod requests).
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           networks. Confirm both are created.
        3. Create a cold Plan for the VM with the selected worker's built-in
           kubernetes.io/hostname label as the convertor node selector and the
           chosen cluster instancetype as the VM's instanceType. Check the Plan's
           Ready condition is True and that its spec contains the convertor node
           selector and the VM's instanceType.
        4. Run the migration. While it is running, confirm the virt-v2v convertor
           pod carries the Plan's convertor node selector and is scheduled on the
           selected worker. Check the migration reaches Succeeded.
        5. Verify on the destination VM: it is powered on; its spec.instancetype
           references the chosen instancetype; its template carries no CPU or
           memory overrides; and the vCPU and memory reported by its VMI match the
           instancetype, not the source VM. Run the standard migrated-VM checks,
           then delete the migrated VM; the maps and Plan are
           removed with the run.

    Expected result:
        The migration succeeds, the convertor pod is observed during the migration
        carrying the convertor node selector and running on the selected worker, and
        the migrated VM's CPU and memory come from the configured instancetype with
        no source-derived overrides. On failure, inspect the Migration CR status
        and its VM entries for the failed step, the Plan conditions and its
        convertorNodeSelector and vms[].instanceType fields, the convertor pod spec
        (nodeSelector, nodeName) in the Plan target namespace, and the migrated
        VM spec.instancetype and template domain.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    # Test-config and fixture payloads are external data whose value shapes are not
    # statically known, which is the justified use of Any for their containers.
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
        """Create the StorageMap for the source VM.

        Args:
            prepared_plan: Plan configuration with the prepared source VM.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: OpenShift destination provider.
            source_provider_inventory: Forklift inventory for the source VM.
            target_namespace: Namespace for migration resources.

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
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create the NetworkMap for the source VM.

        Args:
            prepared_plan: Plan configuration with the prepared source VM.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: OpenShift destination provider.
            source_provider_inventory: Forklift inventory for the source VM.
            target_namespace: Namespace for migration resources.
            multus_network_name: Multus network name for network mapping.

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
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        selected_worker_node: dict[str, str],
    ) -> None:
        """Create the cold Plan with the convertor node selector and the VM's instanceType.

        Args:
            prepared_plan: Plan configuration with the prepared source VM.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: OpenShift destination provider.
            target_namespace: Namespace for migration resources.
            source_provider_inventory: Forklift inventory for the source VM.
            selected_worker_node: Selected worker node (node_name and its kubernetes.io/hostname label value) with the most requestable memory, used for convertor pod placement.

        Raises:
            AssertionError: If Plan creation fails or the Plan spec misses a configured setting.
        """
        populate_vm_ids(prepared_plan, source_provider_inventory)

        convertor_node_selector = {"kubernetes.io/hostname": selected_worker_node["hostname_label"]}

        self.__class__.plan_resource = create_plan_resource(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.__class__.storage_map,
            network_map=self.__class__.network_map,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_power_state=prepared_plan["target_power_state"],
            warm_migration=prepared_plan["warm_migration"],
            convertor_node_selector=convertor_node_selector,
        )
        assert self.__class__.plan_resource, "Plan creation failed"

        plan_spec: dict[str, Any] = self.__class__.plan_resource.instance.spec.to_dict()
        assert plan_spec.get("convertorNodeSelector") == convertor_node_selector, (
            f"Plan convertorNodeSelector is '{plan_spec.get('convertorNodeSelector')}', "
            f"expected '{convertor_node_selector}'"
        )
        plan_vm = plan_spec["vms"][0]
        assert plan_vm.get("instanceType") == prepared_plan["virtual_machines"][0]["instance_type"], (
            f"Plan vms[0].instanceType is '{plan_vm.get('instanceType')}', expected "
            f"'{prepared_plan['virtual_machines'][0]['instance_type']}'"
        )

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
        selected_worker_node: dict[str, str],
    ) -> None:
        """Execute the migration while verifying convertor pod selector and placement.

        The convertor pod is short-lived, so its node selector and node placement are
        checked from a status-poll callback while the migration is running.

        Args:
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            target_namespace: Namespace for migration resources.
            selected_worker_node: Selected worker node (node_name and its kubernetes.io/hostname label value) with the most requestable memory that the convertor pod must run on.

        Raises:
            AssertionError: If no convertor pod is observed on the selected worker.
        """
        convertor_node_selector = {"kubernetes.io/hostname": selected_worker_node["hostname_label"]}
        with ConvertorPodObserver(
            plan=self.__class__.plan_resource,
            expected_node_name=selected_worker_node["node_name"],
            expected_node_selector=convertor_node_selector,
        ) as convertor_pod_observer:
            execute_migration(
                ocp_admin_client=ocp_admin_client,
                fixture_store=fixture_store,
                plan=self.__class__.plan_resource,
                target_namespace=target_namespace,
                on_status_poll=convertor_pod_observer,
            )
            convertor_pod_observer.assert_convertor_pod_observed()

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager",
    ) -> None:
        """Run standard checks plus the instanceType verification on the migrated VM.

        Args:
            prepared_plan: Plan configuration with the migrated VM.
            source_provider: Registered vSphere source provider.
            destination_provider: OpenShift destination provider.
            source_provider_data: Source provider credentials and settings.
            source_vms_namespace: Namespace used for source VM inventory.
            source_provider_inventory: Forklift inventory for the source VM.
            vm_ssh_connections: SSH connection manager for migrated VMs.
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
            per_vm_extra_checks=[check_vm_instance_type],
        )
