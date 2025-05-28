from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from kubernetes.dynamic import DynamicClient
from ocp_resources.migration import Migration
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine import VirtualMachine
from pytest import FixtureRequest
from pytest_testconfig import py_config
from simple_logger.logger import get_logger
from timeout_sampler import retry

from exceptions.exceptions import MigrationPlanExecError, MigrationPlanExecStopError
from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.openshift import OCPProvider
from report import create_migration_scale_report
from utilities.migration_utils import prepare_migration_for_tests
from utilities.post_migration import check_vms
from utilities.resources import create_and_store_resource
from utilities.utils import gen_network_map_list, generate_name_with_uuid, get_value_from_py_config

LOGGER = get_logger(__name__)


def migrate_vms(
    request: FixtureRequest,
    ocp_admin_client: DynamicClient,
    source_provider: BaseProvider,
    destination_provider: OCPProvider,
    plan: dict[str, Any],
    network_migration_map: NetworkMap,
    storage_migration_map: StorageMap,
    source_provider_data: dict[str, Any],
    target_namespace: str,
    session_uuid: str,
    fixture_store: Any,
    source_vms_namespace: str,
    source_provider_inventory: ForkliftInventory | None = None,
    cut_over: datetime | None = None,
    pre_hook_name: str | None = None,
    pre_hook_namespace: str | None = None,
    after_hook_name: str | None = None,
    after_hook_namespace: str | None = None,
) -> None:
    warm_migration = plan.get("warm_migration", False)

    run_migration_kwargs = prepare_migration_for_tests(
        plan=plan,
        warm_migration=warm_migration,
        request=request,
        source_provider=source_provider,
        destination_provider=destination_provider,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        target_namespace=target_namespace,
        session_uuid=session_uuid,
        fixture_store=fixture_store,
        cut_over=cut_over,
        pre_hook_name=pre_hook_name,
        pre_hook_namespace=pre_hook_namespace,
        after_hook_name=after_hook_name,
        after_hook_namespace=after_hook_namespace,
        source_vms_namespace=source_vms_namespace,
    )
    try:
        migration_plan = run_migration(**run_migration_kwargs)

        wait_for_migration_complate(plan=migration_plan)

        if py_config.get("create_scale_report"):
            create_migration_scale_report(plan_resource=plan)

        if get_value_from_py_config("check_vms_signals") and plan.get("check_vms_signals", True):
            check_vms(
                plan=plan,
                source_provider=source_provider,
                source_provider_data=source_provider_data,
                destination_provider=destination_provider,
                destination_namespace=target_namespace,
                network_map_resource=network_migration_map,
                storage_map_resource=storage_migration_map,
                target_namespace=target_namespace,
                source_provider_inventory=source_provider_inventory,
            )
    finally:
        if not request.session.config.getoption("skip_teardown"):
            # delete all vms created by the migration to free up space on ceph storage.
            delete_all_vms(ocp_admin_client=ocp_admin_client, namespace=target_namespace)


def run_migration(
    name: str,
    source_provider_name: str,
    source_provider_namespace: str,
    destination_provider_name: str,
    destination_provider_namespace: str,
    storage_map_name: str,
    storage_map_namespace: str,
    network_map_name: str,
    network_map_namespace: str,
    virtual_machines_list: list,
    target_namespace: str,
    warm_migration: bool,
    pre_hook_name: str,
    pre_hook_namespace: str,
    after_hook_name: str,
    after_hook_namespace: str,
    cut_over: datetime,
    fixture_store: Any,
    session_uuid: str,
    test_name: str,
) -> Plan:
    """
    Creates and Runs a Migration ToolKit for Virtualization (MTV) Migration Plan.

    Args:
         name (str): A prefix to use in MTV Resource names.
         source_provider_name (str): Source Provider Resource Name.
         source_provider_namespace (str): Source Provider Resource Namespace.
         destination_provider_name (str): Destination Provider Resource Name.
         destination_provider_namespace (str): Destination Provider Resource Namespace.
         storage_map_name (str): Storage Mapping Name
         storage_map_namespace (str): Storage Mapping Namespace
         network_map_name (str): Network Mapping Name
         network_map_namespace (str): Network Mapping Namespace
         virtual_machines_list (array): an array of PlanVirtualMachineItem).
         target_namespace (str): destination provider target namespace
         warm_migration (bool): Warm Migration.
         cut_over (datetime): Finalize time (warm migration only).
         teardown (bool): Remove the MTV Resources.
         expected_plan_ready (bool): Migration CR should be created
         condition_category (str): Plan's condition category to wait for
         condition_status (str): Plan's condition status to wait for
         condition_type (str): Plan's condition type to wait for

    Returns:
        Plan and Migration Managed Resources.
    """
    plan = create_and_store_resource(
        fixture_store=fixture_store,
        test_name=test_name,
        session_uuid=session_uuid,
        resource=Plan,
        name=name,
        namespace=target_namespace,
        source_provider_name=source_provider_name,
        source_provider_namespace=source_provider_namespace or target_namespace,
        destination_provider_name=destination_provider_name,
        destination_provider_namespace=destination_provider_namespace or target_namespace,
        storage_map_name=storage_map_name,
        storage_map_namespace=storage_map_namespace,
        network_map_name=network_map_name,
        network_map_namespace=network_map_namespace,
        virtual_machines_list=virtual_machines_list,
        target_namespace=target_namespace,
        warm_migration=warm_migration,
        pre_hook_name=pre_hook_name,
        pre_hook_namespace=pre_hook_namespace,
        after_hook_name=after_hook_name,
        after_hook_namespace=after_hook_namespace,
    )

    plan.wait_for_condition(condition=plan.Condition.READY, status=plan.Condition.Status.TRUE, timeout=360)
    create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=Migration,
        name=f"{name}-migration",
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        cut_over=cut_over,
    )
    return plan


def get_vm_suffix() -> str:
    vm_suffix = ""

    if get_value_from_py_config("matrix_test"):
        storage_name = py_config["storage_class"]

        if "ceph-rbd" in storage_name:
            vm_suffix = "-ceph-rbd"

        elif "nfs" in storage_name:
            vm_suffix = "-nfs"

    if get_value_from_py_config("release_test"):
        ocp_version = py_config["target_ocp_version"].replace(".", "-")
        vm_suffix = f"{vm_suffix}-{ocp_version}"

    return vm_suffix


@retry(wait_timeout=int(py_config.get("plan_wait_timeout", 600)), sleep=1, exceptions_dict={MigrationPlanExecError: []})
def wait_for_migration_complate(plan: Plan) -> bool:
    err = "Plan {name} failed to reach the expected condition. \nstatus:\n\t{instance}"
    for cond in plan.instance.status.conditions:
        if cond["category"] == "Advisory":
            if cond["status"] == plan.Condition.Status.TRUE:
                if cond["type"] == plan.Status.SUCCEEDED:
                    return True

                elif cond["type"] == "Failed":
                    raise MigrationPlanExecStopError(err.format(name=plan.name, instance=plan.instance))

    raise MigrationPlanExecError(err.format(name=plan.name, instance=plan.instance))


def get_storage_migration_map(
    fixture_store: dict[str, Any],
    session_uuid: str,
    target_namespace: str,
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    ocp_admin_client: DynamicClient,
    source_provider_inventory: ForkliftInventory,
    vms: list[str],
) -> StorageMap:
    storage_migration_map = source_provider_inventory.vms_storages_mappings(vms=vms)
    storage_map_list: list[dict[str, Any]] = []
    storage_map_from_config: str = py_config["storage_class"]
    for storage in storage_migration_map:
        storage_map_list.append({
            "destination": {"storageClass": storage_map_from_config},
            "source": storage,
        })

    name = generate_name_with_uuid(
        name=f"{source_provider.ocp_resource.name}-{destination_provider.ocp_resource.name}-{storage_map_from_config}-storage-map"
    )
    storage_map = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=StorageMap,
        client=ocp_admin_client,
        name=name,
        namespace=target_namespace,
        mapping=storage_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_provider.ocp_resource.name,
        destination_provider_namespace=destination_provider.ocp_resource.namespace,
    )
    return storage_map


def get_network_migration_map(
    fixture_store: dict[str, Any],
    session_uuid: str,
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    multus_network_name: str,
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    source_provider_inventory: ForkliftInventory,
    vms: list[str],
) -> NetworkMap:
    network_map_list = gen_network_map_list(
        target_namespace=target_namespace,
        source_provider_inventory=source_provider_inventory,
        multus_network_name=multus_network_name,
        vms=vms,
    )
    name = generate_name_with_uuid(
        name=f"{source_provider.ocp_resource.name}-{destination_provider.ocp_resource.name}-network-map"
    )
    network_map = create_and_store_resource(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        resource=NetworkMap,
        client=ocp_admin_client,
        name=name,
        namespace=target_namespace,
        mapping=network_map_list,
        source_provider_name=source_provider.ocp_resource.name,
        source_provider_namespace=source_provider.ocp_resource.namespace,
        destination_provider_name=destination_provider.ocp_resource.name,
        destination_provider_namespace=destination_provider.ocp_resource.namespace,
    )
    return network_map


def create_storagemap_and_networkmap(
    plan: dict,
    fixture_store: dict[str, Any],
    session_uuid: str,
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    ocp_admin_client: DynamicClient,
    multus_network_name: str,
    target_namespace: str,
) -> tuple[StorageMap, NetworkMap]:
    vms = [vm["name"] for vm in plan["virtual_machines"]]
    storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        target_namespace=target_namespace,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        vms=vms,
    )

    network_migration_map = get_network_migration_map(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        vms=vms,
    )
    return storage_migration_map, network_migration_map


def delete_all_vms(ocp_admin_client: DynamicClient, namespace: str) -> None:
    for vm in VirtualMachine.get(dyn_client=ocp_admin_client, namespace=namespace):
        with contextlib.suppress(Exception):
            vm.clean_up(wait=True)
