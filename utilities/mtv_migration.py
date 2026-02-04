from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ocp_resources.migration import Migration
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
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
from utilities.copyoffload_migration import wait_for_plan_secret
from utilities.resources import create_and_store_resource
from utilities.utils import gen_network_map_list

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)


def _find_migration_for_plan(plan: Plan) -> Migration:
    """Find Migration CR for Plan.

    Args:
        plan (Plan): The Plan resource

    Returns:
        Migration: The Migration CR owned by the Plan

    Raises:
        MigrationNotFoundError: If Migration CR cannot be found
    """
    for migration in Migration.get(client=plan.client, namespace=plan.namespace):
        if migration.instance.metadata.ownerReferences:
            for owner_ref in migration.instance.metadata.ownerReferences:
                if owner_ref.get("kind") == "Plan" and owner_ref.get("name") == plan.name:
                    return migration

    raise MigrationNotFoundError(f"Migration CR not found for Plan '{plan.name}' in namespace '{plan.namespace}'")


def _get_failed_migration_step(plan: Plan, vm_name: str) -> str:
    """Get step where VM migration failed.

    Examines the Migration status (not Plan) to find which pipeline step failed.
    The Migration CR contains the detailed VM pipeline execution status.

    Args:
        plan (Plan): The Plan resource (used to find the associated Migration)
        vm_name (str): Name of the VM to check (matches against status.vms[].name or id)

    Returns:
        str: Name of the failed step (e.g., "PreHook", "PostHook", "DiskTransfer")

    Raises:
        MigrationNotFoundError: If Migration CR cannot be found for the Plan
        MigrationStatusError: If Migration has no status or no vms in status
        VmPipelineError: If VM has no pipeline or no failed step in pipeline
        VmNotFoundError: If VM not found in Migration status
    """
    migration = _find_migration_for_plan(plan)

    if not hasattr(migration.instance, "status") or not migration.instance.status:
        raise MigrationStatusError(migration_name=migration.name)

    vms_status = getattr(migration.instance.status, "vms", None)
    if not vms_status:
        raise MigrationStatusError(migration_name=migration.name)

    for vm_status in vms_status:
        vm_id = getattr(vm_status, "id", "")
        vm_status_name = getattr(vm_status, "name", "")

        if vm_name not in (vm_id, vm_status_name):
            continue

        pipeline = getattr(vm_status, "pipeline", None)
        if not pipeline:
            raise VmPipelineError(vm_name=vm_name)

        for step in pipeline:
            step_error = getattr(step, "error", None)
            if step_error:
                step_name = step.name
                LOGGER.info(f"VM {vm_name} failed at step '{step_name}': {step_error}")
                return step_name

        raise VmPipelineError(vm_name=vm_name)

    raise VmNotFoundError(f"VM {vm_name} not found in Migration {migration.name} status")


def _get_all_vms_failed_steps(plan_resource: Plan, vm_names: list[str]) -> dict[str, str | None]:
    """Map VM names to their failed step names.

    Does NOT validate consistency - returns all results. Caller should validate
    if all VMs must fail at same step.

    Args:
        plan_resource (Plan): The Plan resource to check
        vm_names (list[str]): List of VM names to check

    Returns:
        dict[str, str | None]: Mapping of VM name to failed step name (or None if unknown)
    """
    failed_steps: dict[str, str | None] = {}

    for vm_name in vm_names:
        try:
            failed_steps[vm_name] = _get_failed_migration_step(plan_resource, vm_name)
        except (MigrationNotFoundError, MigrationStatusError, VmPipelineError, VmNotFoundError) as e:
            LOGGER.warning(f"Could not get failed step for VM '{vm_name}': {e}")
            failed_steps[vm_name] = None

    return failed_steps


def create_plan_resource(
    ocp_admin_client: DynamicClient,
    fixture_store: dict[str, Any],
    source_provider: BaseProvider,
    destination_provider: OCPProvider,
    storage_map: StorageMap,
    network_map: NetworkMap,
    virtual_machines_list: list[dict[str, Any]],
    target_namespace: str,
    warm_migration: bool = False,
    pre_hook_name: str | None = None,
    pre_hook_namespace: str | None = None,
    after_hook_name: str | None = None,
    after_hook_namespace: str | None = None,
    test_name: str | None = None,
    copyoffload: bool = False,
    preserve_static_ips: bool = False,
    pvc_name_template: str | None = None,
    pvc_name_template_use_generate_name: bool | None = None,
    target_node_selector: dict[str, str] | None = None,
    target_labels: dict[str, str] | None = None,
    target_affinity: dict[str, Any] | None = None,
    vm_target_namespace: str | None = None,
) -> Plan:
    """Create MTV Plan CR resource.

    Creates a Plan Custom Resource for Migration Toolkit for Virtualization (MTV).
    The Plan defines the migration configuration including source/destination providers,
    storage/network mappings, and the list of VMs to migrate.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
        fixture_store (dict[str, Any]): Fixture store for resource tracking and cleanup.
        source_provider (BaseProvider): Source provider instance with ocp_resource.
        destination_provider (OCPProvider): Destination provider instance with ocp_resource.
        storage_map (StorageMap): StorageMap resource for storage mappings.
        network_map (NetworkMap): NetworkMap resource for network mappings.
        virtual_machines_list (list[dict[str, Any]]): List of VM configurations to migrate.
        target_namespace (str): Target namespace for migrated VMs.
        warm_migration (bool): Whether this is a warm migration. Defaults to False.
        pre_hook_name (str | None): Pre-migration hook name. Defaults to None.
        pre_hook_namespace (str | None): Pre-migration hook namespace. Defaults to None.
        after_hook_name (str | None): Post-migration hook name. Defaults to None.
        after_hook_namespace (str | None): Post-migration hook namespace. Defaults to None.
        test_name (str | None): Test name for resource naming. Defaults to None.
        copyoffload (bool): Enable copy-offload specific settings. Defaults to False.
        preserve_static_ips (bool): Preserve static IP addresses. Defaults to False.
        pvc_name_template (str | None): PVC naming template. Defaults to None.
        pvc_name_template_use_generate_name (bool | None): Use generateName for PVCs. Defaults to None.
        target_node_selector (dict[str, str] | None): Optional node selector labels for scheduling VMs to specific nodes. Defaults to None.
        target_labels (dict[str, str] | None): Optional custom labels to apply to migrated VM metadata. Defaults to None.
        target_affinity (dict[str, Any] | None): Optional Kubernetes pod affinity/anti-affinity configuration. Defaults to None.
        vm_target_namespace (str | None): Custom target namespace for VMs. Defaults to None.

    Returns:
        Plan: The created Plan CR resource.

    Raises:
        ValueError: If source_provider or destination_provider ocp_resource is not set.
        TimeoutExpiredError: If Plan fails to reach Ready status within timeout.
    """
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

    if not destination_provider.ocp_resource:
        raise ValueError("destination_provider.ocp_resource is not set")

    plan_kwargs: dict[str, Any] = {
        "client": ocp_admin_client,
        "fixture_store": fixture_store,
        "resource": Plan,
        "namespace": target_namespace,
        "source_provider_name": source_provider.ocp_resource.name,
        "source_provider_namespace": source_provider.ocp_resource.namespace,
        "destination_provider_name": destination_provider.ocp_resource.name,
        "destination_provider_namespace": destination_provider.ocp_resource.namespace,
        "storage_map_name": storage_map.name,
        "storage_map_namespace": storage_map.namespace,
        "network_map_name": network_map.name,
        "network_map_namespace": network_map.namespace,
        "virtual_machines_list": virtual_machines_list,
        "target_namespace": vm_target_namespace or target_namespace,
        "warm_migration": warm_migration,
        "pre_hook_name": pre_hook_name,
        "pre_hook_namespace": pre_hook_namespace,
        "after_hook_name": after_hook_name,
        "after_hook_namespace": after_hook_namespace,
        "preserve_static_ips": preserve_static_ips,
        "pvc_name_template": pvc_name_template,
        "pvc_name_template_use_generate_name": pvc_name_template_use_generate_name,
    }

    if test_name:
        plan_kwargs["test_name"] = test_name

    if target_node_selector:
        plan_kwargs["target_node_selector"] = target_node_selector

    if target_labels:
        plan_kwargs["target_labels"] = target_labels

    if target_affinity:
        plan_kwargs["target_affinity"] = target_affinity

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
        LOGGER.error(f"Source provider: {source_provider.ocp_resource.instance}")
        LOGGER.error(f"Destination provider: {destination_provider.ocp_resource.instance}")
        raise

    # Wait for Forklift to create plan-specific secret for copy-offload (race condition)
    if copyoffload:
        wait_for_plan_secret(ocp_admin_client, target_namespace, plan.name)

    return plan


def execute_migration(
    ocp_admin_client: DynamicClient,
    fixture_store: dict[str, Any],
    plan: Plan,
    target_namespace: str,
    cut_over: datetime | None = None,
) -> None:
    """Create Migration CR and wait for completion.

    Creates a Migration Custom Resource that triggers the actual VM migration
    based on the provided Plan, then waits for the migration to complete.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
        fixture_store (dict[str, Any]): Fixture store for resource tracking and cleanup.
        plan (Plan): The Plan CR resource defining the migration configuration.
        target_namespace (str): Target namespace for the Migration CR.
        cut_over (datetime | None): Cut-over datetime for warm migration. Defaults to None.

    Raises:
        MigrationPlanExecError: If migration fails or times out.
    """
    create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Migration,
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        cut_over=cut_over,
    )

    wait_for_migration_complate(plan=plan)


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
    non_xcopy_datastore_id: str | None = None,
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
        Also supports non_xcopy_datastore_id for mixed datastore scenarios where
        some disks are on XCOPY-capable datastores and others are on non-XCOPY
        datastores. The non-XCOPY datastore is still configured with the offload
        plugin to enable XCOPY fallback behavior.

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
        non_xcopy_datastore_id: Non-XCOPY datastore ID for mixed migrations (optional, mapped with offload plugin for fallback support)
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

    if non_xcopy_datastore_id and not datastore_id:
        raise ValueError("non_xcopy_datastore_id requires datastore_id to be set")

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

        # Create a storage map entry for each XCOPY-capable datastore
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
            LOGGER.info(f"Added storage map entry for datastore: {ds_id} with copy-offload")

        # Add non-XCOPY datastore mapping (with offload plugin for fallback)
        if non_xcopy_datastore_id:
            destination_config = {"storageClass": target_storage_class}
            if access_mode:
                destination_config["accessMode"] = access_mode
            if volume_mode:
                destination_config["volumeMode"] = volume_mode
            storage_map_list.append({
                "destination": destination_config,
                "source": {"id": non_xcopy_datastore_id},
                "offloadPlugin": offload_plugin_config,
            })
            LOGGER.info(f"Added non-XCOPY datastore mapping for: {non_xcopy_datastore_id} (with xcopy fallback)")
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
    multus_network_name: dict[str, str],
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


def verify_vm_disk_count(destination_provider, plan, target_namespace):
    """
    Verifies that the number of disks on each migrated VM matches the expected count from the plan.

    Args:
        destination_provider: The provider object for the destination cluster (OCP).
        plan (dict): The test plan dictionary containing VM configuration.
        target_namespace (str): The namespace where the VM was migrated.
    """
    LOGGER.info("Verifying disks on migrated VM in OpenShift.")
    for vm_config in plan["virtual_machines"]:
        source_vm_name = vm_config["name"]

        # Calculate expected disks: 1 base disk + number of disks in "add_disks"
        num_added_disks = len(vm_config.get("add_disks", []))
        expected_disks = 1 + num_added_disks

        LOGGER.info(f"Fetching details for migrated VM: {source_vm_name} in namespace {target_namespace}")
        migrated_vm_info = destination_provider.vm_dict(name=source_vm_name, namespace=target_namespace)
        num_disks_migrated = len(migrated_vm_info.get("disks", []))
        LOGGER.info(f"Found {num_disks_migrated} disks on migrated VM '{source_vm_name}'. Expecting {expected_disks}.")

        assert num_disks_migrated == expected_disks, (
            f"Expected {expected_disks} disks on migrated VM '{source_vm_name}', but found {num_disks_migrated}."
        )
        LOGGER.info(f"Successfully verified {expected_disks} disks on the migrated VM '{source_vm_name}'.")
