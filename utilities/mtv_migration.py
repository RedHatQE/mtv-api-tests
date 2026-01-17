from __future__ import annotations

from datetime import datetime
from typing import Any

from kubernetes.client.exceptions import ApiException
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

from exceptions.exceptions import (
    MigrationNotFoundError,
    MigrationPlanExecError,
    MigrationStatusError,
    VmNotFoundError,
    VmPipelineError,
)
from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.openshift import OCPProvider
from report import create_migration_scale_report
from utilities.copyoffload_migration import wait_for_plan_secret
from utilities.hooks import validate_all_vms_same_step, validate_expected_hook_failure
from utilities.migration_utils import prepare_migration_for_tests
from utilities.post_migration import check_vms
from utilities.resources import create_and_store_resource
from utilities.ssh_utils import SSHConnectionManager
from utilities.utils import gen_network_map_list, get_value_from_py_config

LOGGER = get_logger(__name__)


def _get_all_vms_failed_steps(
    plan_resource: Plan,
    vm_names: list[str],
) -> dict[str, str | None]:
    """
    Get the failed step for all VMs and return a dictionary of results.

    Does NOT validate consistency - returns all results. Caller should validate
    if all VMs must fail at same step.

    Args:
        plan_resource: The Plan resource to check
        vm_names: List of VM names to check

    Returns:
        Dictionary mapping VM names to their failed step names (or None if unknown)
    """
    failed_steps: dict[str, str | None] = {}

    for vm_name in vm_names:
        try:
            failed_step = _get_failed_migration_step(plan_resource, vm_name)
            failed_steps[vm_name] = failed_step
        except (MigrationNotFoundError, MigrationStatusError, VmPipelineError, VmNotFoundError) as e:
            LOGGER.warning("Could not get failed step for VM '%s': %s", vm_name, e)
            failed_steps[vm_name] = None

    return failed_steps


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
    vm_ssh_connections: SSHConnectionManager | None = None,
) -> None:
    # Populate VM IDs from Forklift inventory for all VMs
    # This ensures we always use IDs in the Plan CR (works for all provider types)
    if source_provider_inventory:
        for vm in plan["virtual_machines"]:
            vm_name = vm["name"]
            vm_data = source_provider_inventory.get_vm(vm_name)
            vm["id"] = vm_data["id"]
            LOGGER.info(f"VM '{vm_name}' -> ID '{vm['id']}'")

    # Extract hook references from plan dict (set by plan fixture in conftest.py)
    pre_hook_name = plan.get("_pre_hook_name")
    pre_hook_namespace = plan.get("_pre_hook_namespace")
    after_hook_name = plan.get("_post_hook_name")
    after_hook_namespace = plan.get("_post_hook_namespace")

    # Validate consistency (name and namespace must both be set or both be None)
    if (pre_hook_name is not None) != (pre_hook_namespace is not None):
        raise ValueError(
            "Fixture bug: plan has '_pre_hook_name' but missing '_pre_hook_namespace'. "
            "Both must be set together by the plan fixture."
        )
    if (after_hook_name is not None) != (after_hook_namespace is not None):
        raise ValueError(
            "Fixture bug: plan has '_post_hook_name' but missing '_post_hook_namespace'. "
            "Both must be set together by the plan fixture."
        )

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

    expected_migration_result = plan.get("expected_migration_result", "succeed")

    if expected_migration_result == "fail":
        migration_plan = run_migration(**run_migration_kwargs)
        try:
            wait_for_migration_complate(plan=migration_plan)
            raise AssertionError(
                "Migration was expected to fail but succeeded. Plan config has expected_migration_result='fail'"
            )
        except MigrationPlanExecError:
            LOGGER.info("Migration failed as expected")

            vm_names = [vm["name"] for vm in plan["virtual_machines"]]
            failed_steps = _get_all_vms_failed_steps(
                plan_resource=migration_plan,
                vm_names=vm_names,
            )
            LOGGER.info("Failed steps per VM: %s", failed_steps)

            # Only validate hooks if hooks are configured (at least one hook present)
            has_hooks = pre_hook_name is not None or after_hook_name is not None
            actual_failed_step: str | None

            if has_hooks:
                # Hooks-specific validation
                actual_failed_step = validate_all_vms_same_step(failed_steps)
                validate_expected_hook_failure(
                    actual_failed_step=actual_failed_step,
                    plan_config=plan,
                )

                if actual_failed_step == "PostHook":
                    LOGGER.info("PostHook failure - VMs are migrated, verifying with check_vms()")
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
                        vm_ssh_connections=vm_ssh_connections,
                    )

        return

    # Normal flow - expect success
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
            vm_ssh_connections=vm_ssh_connections,
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
    pre_hook_name: str | None,
    pre_hook_namespace: str | None,
    after_hook_name: str | None,
    after_hook_namespace: str | None,
    cut_over: datetime,
    fixture_store: Any,
    test_name: str,
    copyoffload: bool = False,
    preserve_static_ips: bool = False,
    pvc_name_template: str | None = None,
    pvc_name_template_use_generate_name: bool | None = None,
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
         pre_hook_name (str | None): Name of the pre-hook resource (None if no pre-hook).
         pre_hook_namespace (str | None): Namespace of the pre-hook resource (None if no pre-hook).
         after_hook_name (str | None): Name of the post-hook resource (None if no post-hook).
         after_hook_namespace (str | None): Namespace of the post-hook resource (None if no post-hook).
         cut_over (datetime): Finalize time (warm migration only).
         teardown (bool): Remove the MTV Resources.
         expected_plan_ready (bool): Migration CR should be created
         condition_category (str): Plan's condition category to wait for
         condition_status (str): Plan's condition status to wait for
         condition_type (str): Plan's condition type to wait for
         copyoffload (bool): Enable copy-offload specific settings for the Plan

    Returns:
        Plan and Migration Managed Resources.
    """
    # Build plan kwargs
    plan_kwargs = {
        "client": ocp_admin_client,
        "fixture_store": fixture_store,
        "test_name": test_name,
        "resource": Plan,
        "namespace": target_namespace,
        "source_provider_name": source_provider_name,
        "source_provider_namespace": source_provider_namespace or target_namespace,
        "destination_provider_name": destination_provider_name,
        "destination_provider_namespace": destination_provider_namespace or target_namespace,
        "storage_map_name": storage_map_name,
        "storage_map_namespace": storage_map_namespace,
        "network_map_name": network_map_name,
        "network_map_namespace": network_map_namespace,
        "virtual_machines_list": virtual_machines_list,
        "target_namespace": target_namespace,
        "warm_migration": warm_migration,
        "pre_hook_name": pre_hook_name,
        "pre_hook_namespace": pre_hook_namespace,
        "after_hook_name": after_hook_name,
        "after_hook_namespace": after_hook_namespace,
        "preserve_static_ips": preserve_static_ips,
        "pvc_name_template": pvc_name_template,
        "pvc_name_template_use_generate_name": pvc_name_template_use_generate_name,
    }

    # Add copy-offload specific parameters if enabled
    if copyoffload:
        # Set PVC naming template for copy-offload migrations
        # The volume populator framework requires this to generate consistent PVC names
        # Note: generateName is enabled by default, so Kubernetes adds random suffix automatically
        plan_kwargs["pvc_name_template"] = "pvc"

    plan = create_and_store_resource(**plan_kwargs)

    try:
        plan.wait_for_condition(condition=Plan.Condition.READY, status=Plan.Condition.Status.TRUE, timeout=360)
    except TimeoutExpiredError:
        LOGGER.error(f"Plan {plan.name} failed to reach status {Plan.Condition.Status.TRUE}\n\t{plan.instance}")
        source_provider = Provider(
            client=ocp_admin_client, name=source_provider_name, namespace=source_provider_namespace
        )
        dest_provider = Provider(
            client=ocp_admin_client, name=destination_provider_name, namespace=destination_provider_namespace
        )
        LOGGER.error(f"Source provider: {source_provider.instance}")
        LOGGER.error(f"Destinaion provider: {dest_provider.instance}")
        raise

    # Wait for Forklift to create plan-specific secret for copy-offload (race condition)
    if copyoffload:
        wait_for_plan_secret(ocp_admin_client, target_namespace, plan.name)

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
    storage_class: str | None = None,
    # Copy-offload specific parameters
    datastore_id: str | None = None,
    secondary_datastore_id: str | None = None,
    offload_plugin_config: dict[str, Any] | None = None,
    access_mode: str | None = None,
    volume_mode: str | None = None,
) -> StorageMap:
    """
    Create a storage map for VM migration.

    This function supports both standard migrations and copy-offload migrations.

    Copy-offload migration (extended functionality):
        When datastore_id and offload_plugin_config are provided, creates a
        copy-offload storage map instead of querying the inventory.
        Optionally supports secondary_datastore_id for multi-datastore scenarios.

    Args:
        fixture_store: Pytest fixture store for resource tracking
        target_namespace: Target namespace
        source_provider: Source provider instance
        destination_provider: Destination provider instance
        ocp_admin_client: OpenShift admin client
        source_provider_inventory: Source provider inventory (required for signature compatibility)
        vms: List of VM names (required for signature compatibility)
        storage_class: Storage class to use (optional, defaults to config value)
        datastore_id: Primary datastore ID for copy-offload (optional, triggers copy-offload mode)
        secondary_datastore_id: Secondary datastore ID for multi-datastore copy-offload (optional)
        offload_plugin_config: Copy-offload plugin configuration (optional, required if datastore_id is set)
        access_mode: Access mode for copy-offload (optional, used only in copy-offload mode)
        volume_mode: Volume mode for copy-offload (optional, used only in copy-offload mode)

    Returns:
        StorageMap: Created storage map resource

    Raises:
        ValueError: If required parameters are not provided or invalid
    """
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

    if not destination_provider.ocp_resource:
        raise ValueError("destination_provider.ocp_resource is not set")

    # Determine storage class (from parameter or config)
    target_storage_class: str = storage_class or py_config["storage_class"]

    # Build storage map list based on migration type
    storage_map_list: list[dict[str, Any]] = []

    # Check if copy-offload parameters are provided
    if secondary_datastore_id and not datastore_id:
        raise ValueError("secondary_datastore_id requires datastore_id to be set")

    if datastore_id and not offload_plugin_config:
        raise ValueError("datastore_id requires offload_plugin_config to be set")

    if datastore_id and offload_plugin_config:
        # Copy-offload migration mode
        datastores_to_map = [datastore_id]
        if secondary_datastore_id:
            datastores_to_map.append(secondary_datastore_id)
            LOGGER.info(f"Creating copy-offload storage map for primary and secondary datastores: {datastores_to_map}")
        else:
            LOGGER.info(f"Creating copy-offload storage map for primary datastore: {datastore_id}")

        # Create a storage map entry for each datastore
        for ds_id in datastores_to_map:
            destination_config = {
                "storageClass": target_storage_class,
            }

            # Add copy-offload specific destination settings
            if access_mode:
                destination_config["accessMode"] = access_mode
            if volume_mode:
                destination_config["volumeMode"] = volume_mode

            storage_map_list.append({
                "destination": destination_config,
                "source": {"id": ds_id},
                "offloadPlugin": offload_plugin_config,
            })
            LOGGER.info(f"Added storage map entry for datastore: {ds_id}")
    else:
        LOGGER.info(f"Creating standard storage map for VMs: {vms}")
        storage_migration_map = source_provider_inventory.vms_storages_mappings(vms=vms)
        for storage in storage_migration_map:
            storage_map_list.append({
                "destination": {"storageClass": target_storage_class},
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


def verify_vm_disk_count(destination_provider, plan, target_namespace):
    """
    Verifies that the number of disks on the migrated VM matches the expected count from the plan.

    Args:
        destination_provider: The provider object for the destination cluster (OCP).
        plan (dict): The test plan dictionary containing VM configuration.
        target_namespace (str): The namespace where the VM was migrated.
    """
    LOGGER.info("Verifying disks on migrated VM in OpenShift.")
    vm_config = plan["virtual_machines"][0]
    source_vm_name = vm_config["name"]

    # Calculate expected disks: 1 base disk + number of disks in "add_disks"
    num_added_disks = len(vm_config.get("add_disks", []))
    expected_disks = 1 + num_added_disks

    LOGGER.info(f"Fetching details for migrated VM: {source_vm_name} in namespace {target_namespace}")
    migrated_vm_info = destination_provider.vm_dict(name=source_vm_name, namespace=target_namespace)
    num_disks_migrated = len(migrated_vm_info.get("disks", []))
    LOGGER.info(f"Found {num_disks_migrated} disks on migrated VM '{source_vm_name}'. Expecting {expected_disks}.")

    assert num_disks_migrated == expected_disks, (
        f"Expected {expected_disks} disks on migrated VM, but found {num_disks_migrated}."
    )
    LOGGER.info(f"Successfully verified {expected_disks} disks on the migrated VM.")


def _find_migration_for_plan(plan: Plan) -> Migration:
    """Find the Migration CR associated with a Plan.

    Args:
        plan: The Plan resource

    Returns:
        Migration resource for the plan (most recent if multiple exist)

    Raises:
        MigrationNotFoundError: If no Migration found
        ApiException: On Kubernetes API errors (non-404)
    """
    migrations = []
    try:
        for migration_obj in Migration.get(dyn_client=plan.client, namespace=plan.namespace):
            plan_ref = migration_obj.instance.get("spec", {}).get("plan", {})
            if plan_ref.get("name") == plan.name and plan_ref.get("namespace") == plan.namespace:
                migrations.append(migration_obj)
    except ApiException as e:
        if e.status == 404:
            raise MigrationNotFoundError(plan_name=plan.name) from e
        LOGGER.exception(
            "Kubernetes API error getting Migrations for Plan %s in namespace %s: %s",
            plan.name,
            plan.namespace,
            e,
        )
        raise

    if not migrations:
        raise MigrationNotFoundError(plan_name=plan.name)

    if len(migrations) > 1:
        LOGGER.warning("Found %s Migrations for Plan %s, using the most recent", len(migrations), plan.name)
        migrations.sort(
            key=lambda m: datetime.fromisoformat(
                m.instance.get("metadata", {}).get("creationTimestamp", "1970-01-01T00:00:00Z").replace("Z", "+00:00")
            )
        )

    return migrations[-1]


def _get_failed_migration_step(plan: Plan, vm_name: str) -> str:
    """Get the step name where migration failed for a specific VM.

    Examines the Migration status (not Plan) to find which pipeline step failed.
    The Migration CR contains the detailed VM pipeline execution status.

    Args:
        plan: The Plan resource (used to find the associated Migration)
        vm_name: Name of the VM to check (matches against status.vms[].name or id)

    Returns:
        The failed step name (e.g., "PreHook", "PostHook", "DiskTransfer")

    Raises:
        MigrationNotFoundError: If Migration CR cannot be found for the Plan
        MigrationStatusError: If Migration has no status or no vms in status
        VmPipelineError: If VM has no pipeline or no failed step in pipeline
        VmNotFoundError: If VM not found in Migration status
        ApiException: On Kubernetes API errors (non-404)

    Example:
        >>> failed_step = _get_failed_migration_step(plan, "my-vm")
        >>> assert failed_step == "PostHook", f"Expected PostHook failure, got {failed_step}"
    """
    # Find the Migration CR for this Plan
    migration = _find_migration_for_plan(plan)

    if not hasattr(migration.instance, "status") or not migration.instance.status:
        raise MigrationStatusError(migration_name=migration.name)

    vms_status = getattr(migration.instance.status, "vms", None)
    if not vms_status:
        raise MigrationStatusError(migration_name=migration.name)

    for vm_status in vms_status:
        # Match by name or id
        vm_id = getattr(vm_status, "id", "")
        vm_status_name = getattr(vm_status, "name", "")

        if vm_name not in (vm_id, vm_status_name):
            continue

        # Check pipeline steps for errors
        pipeline = getattr(vm_status, "pipeline", None)
        if not pipeline:
            raise VmPipelineError(vm_name=vm_name)

        for step in pipeline:
            step_error = getattr(step, "error", None)
            if step_error:
                step_name = step.name
                LOGGER.info("VM %s failed at step '%s': %s", vm_name, step_name, step_error)
                return step_name

        raise VmPipelineError(vm_name=vm_name)

    raise VmNotFoundError(f"VM {vm_name} not found in Migration {migration.name} status")
