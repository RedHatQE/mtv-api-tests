from __future__ import annotations
from typing import Any, Generator
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytz
from ocp_resources.migration import Migration
from ocp_resources.plan import Plan
from ocp_resources.provider import Provider
from ocp_resources.resource import Resource, ResourceEditor
from pytest_testconfig import py_config

from report import create_migration_scale_report
from utilities.post_migration import check_vms
from utilities.utils import is_true


def get_cutover_value(current_cutover: bool = False) -> datetime:
    datetime_utc = datetime.now(pytz.utc)
    if current_cutover:
        return datetime_utc

    return datetime_utc + timedelta(minutes=int(py_config["mins_before_cutover"]))


def run_cut_over(migration: Migration) -> None:
    ResourceEditor(
        patches={
            migration: {
                "spec": {"cutover": get_cutover_value(current_cutover=True).strftime(format="%Y-%m-%dT%H:%M:%SZ")},
            }
        }
    ).update()


def migrate_vms(
    source_provider,
    destination_provider,
    plans,
    network_migration_map,
    storage_migration_map,
    source_provider_data,
    target_namespace,
    source_provider_host=None,
    cut_over=None,
    pre_hook_name=None,
    pre_hook_namespace=None,
    after_hook_name=None,
    after_hook_namespace=None,
    expected_plan_ready=True,
    condition_status=Resource.Condition.Status.TRUE,
    condition_type=Resource.Status.SUCCEEDED,
) -> None:
    # Allow Running the Post VM Signals Check For VMs that were already imported with an earlier session (API or UI).
    # The VMs are identified by Name Only
    if not is_true(py_config.get("skip_migration")):
        plan_name = f"mtv-api-tests-{datetime.now().strftime('%y-%d-%m-%H-%M-%S')}-{uuid.uuid4().hex[0:3]}"
        plans[0]["name"] = plan_name

        # Plan CR accepts only VM name/id
        virtual_machines_list = [{"name": vm["name"]} for vm in plans[0]["virtual_machines"]]
        if py_config["source_provider_type"] == Provider.ProviderType.OPENSHIFT:
            for idx in range(len(virtual_machines_list)):
                virtual_machines_list[idx].update({"namespace": target_namespace})

        plan_warm_migration = plans[0].get("warm_migration")

        with run_migration(
            name=plan_name,
            namespace=py_config["mtv_namespace"],
            virtual_machines_list=virtual_machines_list,
            warm_migration=plan_warm_migration or bool(py_config["warm_migration"]),
            source_provider_name=source_provider.ocp_resource.name,
            source_provider_namespace=source_provider.ocp_resource.namespace,
            destination_provider_name=destination_provider.ocp_resource.name,
            destination_provider_namespace=destination_provider.ocp_resource.namespace,
            network_map_name=network_migration_map.name,
            network_map_namespace=network_migration_map.namespace,
            storage_map_name=storage_migration_map.name,
            storage_map_namespace=storage_migration_map.namespace,
            target_namespace=target_namespace,
            pre_hook_name=pre_hook_name,
            pre_hook_namespace=pre_hook_namespace,
            after_hook_name=after_hook_name,
            after_hook_namespace=after_hook_namespace,
            teardown=False,
            cut_over=cut_over,
            expected_plan_ready=expected_plan_ready,
            condition_status=condition_status,
            condition_type=condition_type,
        ) as (plan, migration):
            # Warm Migration: Run cut-over after all vms in the plan have more than the underlined number of pre-copies
            if plans[0].get("pre_copies_before_cut_over") and not cut_over and plan_warm_migration:
                source_provider.wait_for_snapshots(
                    vm_names_list=[v["name"] for v in plans[0]["virtual_machines"]],
                    number_of_snapshots=plans[0].get("pre_copies_before_cut_over"),
                )
                if migration:
                    run_cut_over(migration=migration)

        if migration:
            plan.wait_for_condition(
                status=condition_status,
                condition=condition_type,
                timeout=int(py_config.get("plan_wait_timeout", 600)),
            )

            if is_true(py_config.get("create_scale_report")):
                create_migration_scale_report(plan_resource=plan)

    if is_true(py_config.get("check_vms_signals")) and is_true(plans[0].get("check_vms_signals", True)):
        check_vms(
            plan=plans[0],
            source_provider=source_provider,
            source_provider_data=source_provider_data,
            destination_provider=destination_provider,
            destination_namespace=target_namespace,
            network_map_resource=network_migration_map,
            storage_map_resource=storage_migration_map,
            source_provider_host=source_provider_host,
            target_namespace=target_namespace,
        )


@contextmanager
def run_migration(
    name,
    namespace,
    source_provider_name,
    source_provider_namespace,
    destination_provider_name,
    destination_provider_namespace,
    storage_map_name,
    storage_map_namespace,
    network_map_name,
    network_map_namespace,
    virtual_machines_list,
    target_namespace,
    warm_migration,
    pre_hook_name,
    pre_hook_namespace,
    after_hook_name,
    after_hook_namespace,
    teardown,
    cut_over,
    expected_plan_ready,
    condition_status,
    condition_type,
) -> Generator[tuple[Plan, Migration | None], Any, Any]:
    """
    Creates and Runs a Migration ToolKit for Virtualization (MTV) Migration Plan.

    Args:
         name (str): A prefix to use in MTV Resource names.
         namespace (str): MTV namespace.
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
    with Plan(
        name=f"{name}-plan",
        namespace=namespace,
        source_provider_name=source_provider_name,
        source_provider_namespace=source_provider_namespace or namespace,
        destination_provider_name=destination_provider_name,
        destination_provider_namespace=destination_provider_namespace or namespace,
        storage_map_name=storage_map_name,
        storage_map_namespace=storage_map_namespace,
        network_map_name=network_map_name,
        network_map_namespace=network_map_namespace,
        virtual_machines_list=virtual_machines_list,
        target_namespace=target_namespace,
        warm_migration=bool(warm_migration),
        pre_hook_name=pre_hook_name,
        pre_hook_namespace=pre_hook_namespace,
        after_hook_name=after_hook_name,
        after_hook_namespace=after_hook_namespace,
        teardown=teardown,
    ) as plan:
        if expected_plan_ready:
            plan.wait_for_condition(condition=plan.Condition.READY, status=plan.Condition.Status.TRUE, timeout=360)
            with Migration(
                name=f"{name}-migration",
                namespace=namespace,
                plan_name=plan.name,
                plan_namespace=namespace,
                cut_over=cut_over,
                teardown=teardown,
            ) as migration:
                yield plan, migration
        else:
            plan.wait_for_condition(status=condition_status, condition=condition_type, timeout=300)
            yield plan, None


def get_vm_suffix() -> str:
    vm_suffix = ""

    if py_config["matrix_test"]:
        storage_name = py_config["storage_class"]
        if "ceph-rbd" in storage_name:
            vm_suffix = "-ceph-rbd"

        elif "nfs" in storage_name:
            vm_suffix = "-nfs"

    if py_config["release_test"]:
        ocp_version = py_config["target_ocp_version"].replace(".", "-")
        vm_suffix = f"{vm_suffix}-{ocp_version}"

    return vm_suffix
