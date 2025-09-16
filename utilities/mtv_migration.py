from __future__ import annotations

from datetime import datetime
from typing import Any

from kubernetes.dynamic import DynamicClient
from ocp_resources.migration import Migration
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.provider import Provider
from ocp_resources.storage_map import StorageMap
from pytest import FixtureRequest
from pytest_testconfig import py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import MigrationPlanExecError
from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.openshift import OCPProvider
from report import create_migration_scale_report
from utilities.migration_utils import prepare_migration_for_tests
from utilities.post_migration import check_vms
from utilities.resources import create_and_store_resource
from utilities.utils import gen_network_map_list, get_value_from_py_config

LOGGER = get_logger(__name__)


def migrate_vms(
    ocp_admin_client: DynamicClient,
    request: FixtureRequest,
    source_provider: BaseProvider,
    destination_provider: OCPProvider,
    plan: dict[str, Any],
    network_migration_map: NetworkMap,
    storage_migration_map: StorageMap,
    source_provider_data: dict[str, Any],
    target_namespace: str,
    fixture_store: Any,
    source_vms_namespace: str,
    source_provider_inventory: ForkliftInventory | None = None,
    cut_over: datetime | None = None,
    pre_hook_name: str | None = None,
    pre_hook_namespace: str | None = None,
    after_hook_name: str | None = None,
    after_hook_namespace: str | None = None,
) -> None:
    run_migration_kwargs = prepare_migration_for_tests(
        ocp_admin_client=ocp_admin_client,
        plan=plan,
        request=request,
        source_provider=source_provider,
        destination_provider=destination_provider,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        target_namespace=target_namespace,
        fixture_store=fixture_store,
        cut_over=cut_over,
        pre_hook_name=pre_hook_name,
        pre_hook_namespace=pre_hook_namespace,
        after_hook_name=after_hook_name,
        after_hook_namespace=after_hook_namespace,
        source_vms_namespace=source_vms_namespace,
    )
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
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
        )


def run_migration(
    ocp_admin_client: DynamicClient,
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
        client=ocp_admin_client,
        fixture_store=fixture_store,
        test_name=test_name,
        resource=Plan,
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

    try:
        plan.wait_for_condition(condition=Plan.Condition.READY, status=Plan.Condition.Status.TRUE, timeout=360)
    except TimeoutExpiredError:
        LOGGER.error(f"Plan {plan.name} failed to reach status {Plan.Condition.Status.TRUE}\n\t{plan.instance}")
        source_provider = Provider(name=source_provider_name, namespace=source_provider_namespace)
        dest_provider = Provider(name=destination_provider_name, namespace=destination_provider_namespace)
        LOGGER.error(f"Source provider: {source_provider.instance}")
        LOGGER.error(f"Destinaion provider: {dest_provider.instance}")
        raise
    create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Migration,
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        cut_over=cut_over,
    )
    return plan


def get_vm_suffix(warm_migration: bool) -> str:
    migration_type = "warm" if warm_migration else "cold"
    storage_class = py_config.get("storage_class", "")
    storage_class_name = "-".join(storage_class.split("-")[-2:])
    ocp_version = py_config.get("target_ocp_version", "").replace(".", "-")

    vm_suffix = f"-{storage_class_name}-{ocp_version}-{migration_type}"
    if len(vm_suffix) > 63:
        LOGGER.warning(f"VM suffix '{vm_suffix}' is too long ({len(vm_suffix)} > 63). Truncating.")
        vm_suffix = vm_suffix[-63:]

    return vm_suffix


def wait_for_migration_complate(plan: Plan) -> None:
    def _wait_for_migration_complate(_plan: Plan) -> str:
        for cond in _plan.instance.status.conditions:
            if cond["category"] == "Advisory" and cond["status"] == Plan.Condition.Status.TRUE:
                cond_type = cond["type"]

                if cond_type in (Plan.Status.SUCCEEDED, Plan.Status.FAILED):
                    return cond_type

        return "Executing"

    try:
        last_status: str = ""

        for sample in TimeoutSampler(
            func=_wait_for_migration_complate,
            sleep=1,
            wait_timeout=py_config.get("plan_wait_timeout", 600),
            _plan=plan,
        ):
            if sample != last_status:
                LOGGER.info(f"Plan '{plan.name}' migration status: '{sample}'")
                last_status = sample

            if sample == Plan.Status.SUCCEEDED:
                return

            elif sample == Plan.Status.FAILED:
                raise MigrationPlanExecError()

    except (TimeoutExpiredError, MigrationPlanExecError):
        raise MigrationPlanExecError(
            f"Plan {plan.name} failed to reach the expected condition. \nstatus:\n\t{plan.instance}"
        )


def get_storage_migration_map(
    fixture_store: dict[str, Any],
    target_namespace: str,
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    ocp_admin_client: DynamicClient,
    source_provider_inventory: ForkliftInventory,
    vms: list[str],
) -> StorageMap:
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

    if not destination_provider.ocp_resource:
        raise ValueError("destination_provider.ocp_resource is not set")

    storage_migration_map = source_provider_inventory.vms_storages_mappings(vms=vms)
    storage_map_list: list[dict[str, Any]] = []
    storage_map_from_config: str = py_config["storage_class"]
    for storage in storage_migration_map:
        storage_map_list.append({
            "destination": {"storageClass": storage_map_from_config},
            "source": storage,
        })

    storage_map = create_and_store_resource(
        fixture_store=fixture_store,
        resource=StorageMap,
        client=ocp_admin_client,
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
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    multus_network_name: str,
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    source_provider_inventory: ForkliftInventory,
    vms: list[str],
) -> NetworkMap:
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

    if not destination_provider.ocp_resource:
        raise ValueError("destination_provider.ocp_resource is not set")

    network_map_list = gen_network_map_list(
        target_namespace=target_namespace,
        source_provider_inventory=source_provider_inventory,
        multus_network_name=multus_network_name,
        vms=vms,
    )
    network_map = create_and_store_resource(
        fixture_store=fixture_store,
        resource=NetworkMap,
        client=ocp_admin_client,
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
        target_namespace=target_namespace,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        vms=vms,
    )

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
    return storage_migration_map, network_migration_map
