from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from subprocess import TimeoutExpired
from time import monotonic
from typing import TYPE_CHECKING, Any

import yaml
from kubernetes.dynamic.exceptions import ApiException, DynamicApiError
from ocp_resources.migration import Migration
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.provider import Provider
from ocp_resources.storage_map import StorageMap
from ocp_resources.virtual_machine_instance import VirtualMachineInstance
from pyhelper_utils.shell import run_command
from pytest_testconfig import py_config
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler
from urllib3.exceptions import MaxRetryError, ProtocolError

from exceptions.exceptions import (
    MigrationNotFoundError,
    MigrationPlanExecError,
    MigrationVmUnschedulableError,
    VmNotFoundError,
    VmPipelineError,
)
from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.openshift import OCPProvider
from utilities.cluster_state import ClusterStateProbeTimeout, dump_cluster_state, read_with_deadline
from utilities.data_collector import data_collector_path, redact_secret, utc_timestamp
from utilities.deep_inspection import get_plan_conversion_crs
from utilities.naming import resolve_destination_vm_name
from utilities.resources import create_and_store_resource
from utilities.utils import gen_network_map_list, resolve_cluster_credential
from utilities.worker_node_selection import has_unschedulable_condition

if TYPE_CHECKING:
    from pathlib import Path

    from kubernetes.dynamic import DynamicClient, ResourceInstance

# Forklift virt-v2v conversion pod label; shared by all conversion-pod observers and helpers.
CONVERSION_POD_LABEL_SELECTOR: str = "forklift.app=virt-v2v"

# Test-config keys renamed to their Kubernetes API spec field names.
_VM_CONFIG_TO_SPEC_KEY_RENAMES: dict[str, str] = {
    "migrate_shared_disks": "migrateSharedDisks",
    "instance_type": "instanceType",
    "scsi_reservation": "scsiReservation",
}

LOGGER = get_logger(__name__)

_VM_DICT_TEST_ONLY_KEYS: frozenset[str] = frozenset({
    "add_nic",
    "add_nic_start_connected",
    "clone_name",
    "connected_nic_mac",
    "disconnected_nic_mac",
    "preserve_name_format",
    "snapshots_before_migration",
})

# Keys consumed directly by create_and_store_resource that create_plan_resource does not
# set itself. Passing any of these via additional_plan_options could make create_and_store_resource
# reuse an existing resource (e.g. an already-existing Plan name) instead of creating a new one,
# so they are reserved and rejected by the duplicate-options check below.
_CREATE_AND_STORE_RESOURCE_KEYS: frozenset[str] = frozenset({
    "name",
    "kind_dict",
    "yaml_file",
    "test_name",
})

# A migrated VM the scheduler has definitively rejected never starts on its own, and the suite
# never frees destination node capacity, so a confirmed Unschedulable condition fails the run
# instead of burning the whole plan timeout.
_UNSCHEDULABLE_CHECK_INTERVAL_SECONDS = 30
_UNSCHEDULABLE_CONFIRMATIONS_REQUIRED = 3

# MTV deletes the failed objects (``deleteVmOnFailMigration``) before must-gather runs, so the
# evidence of a failed plan is captured on the Failed poll, while it still exists.
_FAILURE_EVIDENCE_DIR_PREFIX = "failed-plan"
_FAILURE_EVIDENCE_POD_MARKERS = ("deep-inspect", "convertor", "importer", "populator", "virt-v2v")
_FAILURE_EVIDENCE_LOG_TAIL_LINES = 200
_FAILURE_EVIDENCE_COMMAND_TIMEOUT_SECONDS = 60
# MTV deletes the Conversion CR before the Plan reports Failed, so the CR is also sampled while
# the plan runs, and the whole capture is capped so a slow cluster cannot delay the failure.
_FAILURE_EVIDENCE_SNAPSHOT_INTERVAL_SECONDS = 10
_FAILURE_EVIDENCE_TOTAL_TIMEOUT_SECONDS = 120
_FAILURE_EVIDENCE_NO_CONVERSIONS_MARKER = (
    "# No Conversion CR for this plan was found at capture time or on any earlier poll:\n"
    "# MTV deletes it on failure, so the failure reason is in the pod logs and events here.\n"
)


def get_migration_for_plan(plan: Plan) -> Migration:
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


def get_conversion_pods(
    client: "DynamicClient",
    namespace: str,
    migration_uid: str,
) -> list["ResourceInstance"]:
    """List raw virt-v2v conversion pod snapshots for one migration.

    Args:
        client (DynamicClient): OpenShift client used to query Pods.
        namespace (str): Namespace the conversion pods run in (the Plan target namespace).
        migration_uid (str): UID of the Migration whose conversion pods are listed.

    Returns:
        list[ResourceInstance]: Raw Pod snapshots labeled for the given migration.
    """
    return list(
        Pod.get(
            client=client,
            namespace=namespace,
            label_selector=f"{CONVERSION_POD_LABEL_SELECTOR},migration={migration_uid}",
            raw=True,
        )
    )


def _get_failed_migration_step(plan: Plan, vm_name: str) -> str:
    """Get step where VM migration failed.

    Reads pipeline data from the Plan CR status, which is the authoritative source.
    The Plan CR is updated by the forklift controller before the Migration CR syncs,
    avoiding race conditions where the Migration CR has not yet reflected pipeline errors.

    Args:
        plan (Plan): The Plan resource with migration status
        vm_name (str): Name of the VM to check (matches against status.vms[].name or id)

    Returns:
        str: Name of the failed step (e.g., "PreHook", "PostHook", "DiskTransfer")

    Raises:
        VmPipelineError: If VM has no pipeline or no failed step in pipeline
        VmNotFoundError: If VM not found in Plan migration status
    """
    vms_status = plan.instance.status.migration.vms

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

    raise VmNotFoundError(f"VM '{vm_name}' not found in Plan '{plan.name}' migration status")


def _get_all_vms_failed_steps(plan_resource: Plan, vm_names: list[str]) -> dict[str, str]:
    """Map VM names to their failed step names.

    Does NOT validate consistency - caller should validate if all VMs must fail
    at same step. Raises on the first VM where the failed step cannot be determined.

    Args:
        plan_resource (Plan): The Plan resource to check
        vm_names (list[str]): List of VM names to check

    Returns:
        dict[str, str]: Mapping of VM name to failed step name

    Raises:
        VmPipelineError: If VM pipeline is missing or has no failed step
        VmNotFoundError: If VM not found in Plan migration status
    """
    failed_steps: dict[str, str] = {}

    for vm_name in vm_names:
        failed_steps[vm_name] = _get_failed_migration_step(plan_resource, vm_name)

    return failed_steps


def resolve_pvc_name_template(
    pvc_name_template: str | dict[str, str],
    source_provider_type: str,
) -> str:
    """Resolve a provider-appropriate PVC name template.

    Accepts either a ready-to-use template string (returned unchanged) or a
    provider-keyed mapping. For a mapping, the template for
    ``source_provider_type`` is used, falling back to the ``"default"`` key
    when no provider-specific entry exists.

    Args:
        pvc_name_template (str | dict[str, str]): A template string, or a
            mapping of provider type to template string. A mapping must
            contain either a key matching ``source_provider_type`` or a
            ``"default"`` key.
        source_provider_type (str): The source provider type
            (e.g. ``Provider.ProviderType.VSPHERE``).

    Returns:
        str: The resolved PVC name template string.

    Raises:
        ValueError: If the mapping contains a key that is not a valid
            ``Provider.ProviderType`` value or ``"default"``, or if the
            mapping has no entry matching ``source_provider_type`` and no
            ``"default"`` key.
    """
    if isinstance(pvc_name_template, dict):
        valid_provider_types = {
            value
            for name, value in vars(Provider.ProviderType).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        allowed_keys = valid_provider_types | {"default"}

        unknown_keys = set(pvc_name_template) - allowed_keys
        if unknown_keys:
            raise ValueError(
                f"Unknown 'pvc_name_template' key(s): {sorted(unknown_keys)}; allowed keys: {sorted(allowed_keys)}"
            )

        if source_provider_type in pvc_name_template:
            return pvc_name_template[source_provider_type]

        if "default" in pvc_name_template:
            return pvc_name_template["default"]

        raise ValueError(
            f"No 'pvc_name_template' entry found for provider type '{source_provider_type}' and no "
            f"'default' key present; available keys: {sorted(pvc_name_template.keys())}"
        )

    return pvc_name_template


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
    network_name_template: str | None = None,
    pvc_name_template_use_generate_name: bool | None = None,
    target_node_selector: dict[str, str] | None = None,
    target_labels: dict[str, str] | None = None,
    target_affinity: dict[str, Any] | None = None,
    vm_target_namespace: str | None = None,
    migrate_shared_disks: bool | None = None,
    target_power_state: str | None = None,
    enable_nested_virtualization: bool | None = None,
    xfs_compatibility: bool = False,
    run_preflight_inspection: bool | None = None,
    rdm_as_lun: bool | None = None,
    pre_copy_source_disks: bool = False,
    convertor_node_selector: dict[str, str] | None = None,
    # Any: Plan option values are heterogeneous/provider-defined and intentionally untyped to pass arbitrary kubevirt Plan spec fields through.
    additional_plan_options: dict[str, Any] | None = None,
    scsi_reservation: bool | None = None,
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
        network_name_template (str | None): Template for migrated VM network names. Defaults to None.
        pvc_name_template_use_generate_name (bool | None): Use generateName for PVCs. Defaults to None.
        target_node_selector (dict[str, str] | None): Optional node selector labels for scheduling VMs to specific nodes. Defaults to None.
        target_labels (dict[str, str] | None): Optional custom labels to apply to migrated VM metadata. Defaults to None.
        target_affinity (dict[str, Any] | None): Optional Kubernetes pod affinity/anti-affinity configuration. Defaults to None.
        vm_target_namespace (str | None): Custom target namespace for VMs. Defaults to None.
        migrate_shared_disks (bool | None): Whether to migrate shared disks at plan level. True enables shared disk
            migration for all VMs by default. Individual VMs can override via per-VM migrateSharedDisks. Defaults to None.
        target_power_state (str | None): Target power state for VMs after migration (e.g., 'on', 'off'). Defaults to None.
        enable_nested_virtualization (bool | None): Whether to enable nested virtualization on migrated VMs.
            When False, CPU features vmx/svm are disabled. Defaults to None (not set on Plan CR).
        xfs_compatibility (bool): Whether to use XFS-compatible virt-v2v image for VMs with XFS v4 filesystems. Defaults to False.
        run_preflight_inspection (bool | None): Whether to run preflight Deep Inspection on warm migrations.
            Defaults to None (forklift default: True).
        rdm_as_lun (bool | None): Whether to map RDM disks as LUN devices with SCSI bus
            on the target VM. Only applies to vSphere source providers. Defaults to None.
        pre_copy_source_disks (bool): Enable two-phase Hyper-V disk transfer (VHDX to raw via
            qemu-img, then in-place virt-v2v). Only applies to Hyper-V migrations. Defaults to False.
        convertor_node_selector (dict[str, str] | None): Optional selector for virt-v2v conversion and vSphere
            warm-migration CDI importer pods. Defaults to None.
        additional_plan_options (dict[str, Any] | None): Additional Plan constructor options not already handled by
            this helper. Defaults to None. Keys consumed by create_and_store_resource ("name", "kind_dict", "yaml_file",
            "test_name") are not allowed.
        scsi_reservation (bool | None): Whether to enable SCSI persistent reservation on
            shared RDM LUN disks (requires rdm_as_lun=True). Individual VMs can override
            via per-VM scsiReservation. Defaults to None.

    Returns:
        Plan: The created Plan CR resource.

    Raises:
        ValueError: If either provider resource is not set or additional_plan_options conflicts with options already
            set by this helper or with keys consumed by create_and_store_resource (e.g. "name").
        TimeoutExpiredError: If Plan fails to reach Ready status within timeout.
    """
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

    if not destination_provider.ocp_resource:
        raise ValueError("destination_provider.ocp_resource is not set")

    # Convert per-VM snake_case config keys to camelCase API fields.
    # The Plan class passes virtual_machines_list directly to spec.vms, so any key in the
    # VM dict ends up in the CR. We rename here to match the Kubernetes API field names.
    # Work on copies to avoid mutating the caller's prepared_plan data.
    vms_for_plan = [dict(vm) for vm in virtual_machines_list]
    for vm in vms_for_plan:
        for config_key, spec_key in _VM_CONFIG_TO_SPEC_KEY_RENAMES.items():
            if config_key in vm:
                vm[spec_key] = vm.pop(config_key)
        for key in _VM_DICT_TEST_ONLY_KEYS:
            vm.pop(key, None)

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
        "virtual_machines_list": vms_for_plan,
        "target_namespace": vm_target_namespace or target_namespace,
        "warm_migration": warm_migration,
        "pre_hook_name": pre_hook_name,
        "pre_hook_namespace": pre_hook_namespace,
        "after_hook_name": after_hook_name,
        "after_hook_namespace": after_hook_namespace,
        "preserve_static_ips": preserve_static_ips,
        "pvc_name_template": pvc_name_template,
        "network_name_template": network_name_template,
        "pvc_name_template_use_generate_name": pvc_name_template_use_generate_name,
        "target_power_state": target_power_state,
    }

    if test_name:
        plan_kwargs["test_name"] = test_name

    if target_node_selector:
        plan_kwargs["target_node_selector"] = target_node_selector

    if target_labels:
        plan_kwargs["target_labels"] = target_labels

    if target_affinity:
        plan_kwargs["target_affinity"] = target_affinity

    if migrate_shared_disks is not None:
        plan_kwargs["migrate_shared_disks"] = migrate_shared_disks

    if enable_nested_virtualization is not None:
        plan_kwargs["enable_nested_virtualization"] = enable_nested_virtualization

    if rdm_as_lun is not None:
        plan_kwargs["rdm_as_lun"] = rdm_as_lun

    if pre_copy_source_disks:
        plan_kwargs["pre_copy_source_disks"] = pre_copy_source_disks

    if convertor_node_selector is not None:
        plan_kwargs["convertor_node_selector"] = convertor_node_selector

    if scsi_reservation is not None:
        plan_kwargs["scsi_reservation"] = scsi_reservation

    # Add copy-offload specific parameters if enabled
    if copyoffload:
        # Set PVC naming template for copy-offload migrations
        # The volume populator framework requires this to generate consistent PVC names
        # Note: generateName is enabled by default, so Kubernetes adds random suffix automatically
        plan_kwargs["pvc_name_template"] = "pvc"

    if xfs_compatibility:
        plan_kwargs["xfs_compatibility"] = xfs_compatibility

    if run_preflight_inspection is not None:
        plan_kwargs["run_preflight_inspection"] = run_preflight_inspection

    if additional_plan_options:
        duplicate_options = (plan_kwargs.keys() | _CREATE_AND_STORE_RESOURCE_KEYS) & additional_plan_options.keys()
        if duplicate_options:
            raise ValueError(
                f"Additional Plan options cannot override options already set by create_plan_resource: "
                f"{sorted(duplicate_options)}"
            )
        plan_kwargs.update(additional_plan_options)

    plan = create_and_store_resource(**plan_kwargs)

    try:
        plan.wait_for_condition(condition=Plan.Condition.READY, status=Plan.Condition.Status.TRUE, timeout=360)
    except TimeoutExpiredError:
        LOGGER.error(f"Plan {plan.name} failed to reach status {Plan.Condition.Status.TRUE}\n\t{plan.instance}")
        LOGGER.error(f"Source provider: {source_provider.ocp_resource.instance}")
        LOGGER.error(f"Destination provider: {destination_provider.ocp_resource.instance}")
        raise

    return plan


def execute_migration(
    ocp_admin_client: DynamicClient,
    fixture_store: dict[str, Any],
    plan: Plan,
    target_namespace: str,
    cut_over: datetime | None = None,
    on_status_poll: Callable[[str], None] | None = None,
) -> None:
    """Create Migration CR and wait for completion.

    Creates a Migration Custom Resource that triggers the actual VM migration
    based on the provided Plan, then waits for the migration to complete.

    Note: For copy-offload migrations, use execute_copyoffload_migration() from
    utilities.copyoffload_migration instead to ensure populate pod logs are captured.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
        fixture_store (dict[str, Any]): Fixture store for resource tracking and cleanup.
        plan (Plan): The Plan CR resource defining the migration configuration.
        target_namespace (str): Target namespace for the Migration CR.
        cut_over (datetime | None): Cut-over datetime for warm migration. Defaults to None.
        on_status_poll (Callable[[str], None] | None): Optional callback invoked on each poll
            with the current migration status string.

    Raises:
        MigrationPlanExecError: If migration fails or times out.
    """
    create_migration_resource(
        ocp_admin_client=ocp_admin_client,
        fixture_store=fixture_store,
        plan=plan,
        target_namespace=target_namespace,
        cut_over=cut_over,
    )

    wait_for_migration_complate(plan=plan, on_status_poll=on_status_poll)


def create_migration_resource(
    ocp_admin_client: DynamicClient,
    fixture_store: dict[str, Any],
    plan: Plan,
    target_namespace: str,
    cut_over: datetime | None = None,
) -> Migration:
    """Create and track the Migration CR for a Plan.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client for API interactions.
        fixture_store (dict[str, Any]): Fixture store for resource tracking and cleanup.
        plan (Plan): Plan CR that the Migration executes.
        target_namespace (str): Namespace where the Migration CR is created.
        cut_over (datetime | None): Cut-over datetime for warm migration. Defaults to None.

    Returns:
        Migration: The created Migration CR.
    """
    return create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Migration,
        namespace=target_namespace,
        plan_name=plan.name,
        plan_namespace=plan.namespace,
        cut_over=cut_over,
    )


def get_vm_suffix(warm_migration: bool, ocp_version: str) -> str:
    """Generate the VM name suffix for cloned source VMs.

    Args:
        warm_migration (bool): Whether the plan is a warm migration.
        ocp_version (str): Target OpenShift cluster version as "major.minor"
            (e.g. "4.22"), derived from the target cluster.

    Returns:
        str: Suffix encoding storage class, target OCP version and migration
            type (e.g. "-ceph-rbd-4-22-cold"), truncated to 63 characters.
    """
    migration_type = "warm" if warm_migration else "cold"
    storage_class = py_config.get("storage_class", "")
    storage_class_name = "-".join(storage_class.split("-")[-2:])
    vm_suffix = f"-{storage_class_name}-{ocp_version.replace('.', '-')}-{migration_type}"

    if len(vm_suffix) > 63:
        LOGGER.warning(f"VM suffix '{vm_suffix}' is too long ({len(vm_suffix)} > 63). Truncating.")
        vm_suffix = vm_suffix[-63:]

    return vm_suffix


def get_plan_migration_status(plan: Plan) -> str:
    """Get the migration status from the Plan conditions.

    Args:
        plan (Plan): The Plan resource to check.

    Returns:
        str: The status of the plan ("Executing", "Succeeded", "Failed", or empty string if unknown).

    Raises:
        None
    """
    status = plan.instance.status
    if not status:
        return ""

    conditions = status.conditions
    if conditions:
        for cond in conditions:
            if cond["category"] == "Advisory" and cond["status"] == Plan.Condition.Status.TRUE:
                cond_type = cond["type"]

                if cond_type in (Plan.Status.SUCCEEDED, Plan.Status.FAILED):
                    return cond_type

    # Check if Migration CR exists to confirm execution
    try:
        get_migration_for_plan(plan)
        return Plan.Status.EXECUTING
    except MigrationNotFoundError:
        return ""


def _plan_status(plan: Plan) -> dict[str, Any]:
    """Return the Plan status as plain Python data.

    Args:
        plan (Plan): Plan whose status is read.

    Returns:
        dict[str, Any]: Plain-dict view of the Plan status, empty when the Plan reports none.
    """
    # Plan status blocks are free-form provider data, so their values are typed Any: the
    # Kubernetes CRD schema does not express the per-field types the wrapper exposes.
    status = plan.instance.status
    return status.to_dict() if status is not None else {}


def _get_migration_vm_statuses(plan: Plan) -> list[dict[str, Any]]:
    """Return the Plan's per-VM migration status entries.

    Args:
        plan (Plan): Plan whose migration is being waited on.

    Returns:
        list[dict[str, Any]]: Entries of ``status.migration.vms``, empty when the migration
            block is missing or reports no VMs yet.

    Raises:
        ValueError: If the migration block, its ``vms`` list, or a VM entry has an unexpected
            shape.
    """
    migration = _plan_status(plan=plan).get("migration")
    if migration is None:
        return []
    if not isinstance(migration, dict):
        raise ValueError(
            f"Plan '{plan.name}' reports status.migration as {type(migration).__name__}, expected a mapping"
        )

    vms = migration.get("vms")
    if vms is None:
        return []
    if not isinstance(vms, list):
        raise ValueError(f"Plan '{plan.name}' reports status.migration.vms as {type(vms).__name__}, expected a list")

    for vm in vms:
        if not isinstance(vm, dict) or not isinstance(vm.get("name"), str):
            raise ValueError(
                f"Plan '{plan.name}' reports a malformed status.migration.vms entry {vm!r}: "
                "expected a mapping with a string 'name'"
            )

    return vms


def _get_destination_vm_names(plan: Plan) -> set[str]:
    """Collect the destination VM names the plan's migration can produce.

    The migration status only lists VMs that already reported status, so the Plan spec is
    resolved as well: OVA plans rename VMs through ``targetName`` and non-conforming source
    names are sanitized, and either name can be the destination name.

    Args:
        plan (Plan): Plan whose migration is being waited on.

    Returns:
        set[str]: Destination VM names.

    Raises:
        ValueError: If the Plan reports a malformed migration status.
        InvalidVMNameError: If a spec VM name cannot be sanitized to a DNS-1123 name.
    """
    destination_vm_names = {vm["name"] for vm in _get_migration_vm_statuses(plan=plan)}
    # .get() is the only access form that tolerates the optional targetName for both entry shapes
    # the wrapper exposes here (a plain dict or a ResourceField), keeping a VM without targetName
    # from breaking the probe.
    destination_vm_names.update(
        resolve_destination_vm_name({"name": vm.get("name"), "targetName": vm.get("targetName")})
        for vm in plan.instance.spec.vms
    )
    return destination_vm_names


def _get_unschedulable_destination_vm(plan: Plan) -> tuple[str, str] | None:
    """Find a migrated VM the destination scheduler has rejected.

    Args:
        plan (Plan): Plan whose migration is being waited on.

    Returns:
        tuple[str, str] | None: Destination VM name and the raw scheduler message, or None when
            no migrated VM is currently unschedulable or the destination could not be read.

    Raises:
        ValueError: If the Plan reports a malformed migration status or VMI conditions.
    """
    destination_vm_names = _get_destination_vm_names(plan=plan)
    if not destination_vm_names:
        return None

    # A diagnostic read must never abort the migration wait: a transient destination API failure
    # only means there is no scheduler observation on this cycle.
    try:
        for vmi in VirtualMachineInstance.get(
            client=plan.client,
            namespace=plan.instance.spec.targetNamespace,
        ):
            if vmi.name not in destination_vm_names:
                continue

            vmi_status = vmi.instance.status
            conditions = vmi_status.to_dict().get("conditions") if vmi_status is not None else None
            if not has_unschedulable_condition(conditions=conditions):
                continue

            for condition in conditions or []:
                if condition.get("type") == "PodScheduled":
                    return vmi.name, condition.get("message") or condition.get("reason") or "unknown"
    except (ApiException, DynamicApiError) as err:
        LOGGER.info(f"Could not list destination VMIs for plan '{plan.name}', skipping this scheduler check: {err}")
        return None

    return None


def _plan_target_namespace(plan: Plan) -> str | None:
    """Read the migration target namespace without masking the failure being reported.

    Args:
        plan (Plan): Plan whose migration is being waited on.

    Returns:
        str | None: The target namespace, or None when the read failed or the spec carries none.
    """
    try:
        # Bounded read: the wrapper sets no request timeout, so a silent API server must not block
        # the failure path this namespace is needed for.
        return read_with_deadline(probe=lambda: plan.instance.spec.targetNamespace)
    except (
        ApiException,
        DynamicApiError,
        AttributeError,
        MaxRetryError,
        OSError,
        ProtocolError,
        TypeError,
        ValueError,
        ClusterStateProbeTimeout,
    ) as err:
        LOGGER.info(f"Could not read the target namespace of plan '{plan.name}', skipping the snapshot: {err}")
        return None


def _dump_plan_storage_state(plan: Plan, *, reason: str) -> None:
    """Collect a storage snapshot for a plan failure, at no cost when evidence is disabled.

    Args:
        plan (Plan): Plan the failure belongs to.
        reason (str): Short cause, used in the snapshot file name and header.
    """
    # Resolved before the namespace so --skip-data-collector costs no API request either.
    if data_collector_path() is None:
        return
    namespace = _plan_target_namespace(plan=plan)
    if namespace is None:
        return
    dump_cluster_state(
        reason=reason,
        scope="storage",
        namespace=namespace,
        client=plan.client,
        subject=plan.name,
    )


def _confirm_unschedulable_destination_vm(plan: Plan, hits_by_vm: dict[str, int]) -> None:
    """Count consecutive destination scheduler rejections per VM and fail once they are confirmed.

    Args:
        plan (Plan): Plan whose migration is being waited on.
        hits_by_vm (dict[str, int]): Consecutive rejection count per destination VM name,
            updated in place so a VM that schedules again starts over.

    Raises:
        MigrationVmUnschedulableError: If one VM is rejected on enough consecutive checks.
        ValueError: If the Plan reports a malformed migration status.
    """
    unschedulable_vm = _get_unschedulable_destination_vm(plan=plan)
    observed_vm_name = unschedulable_vm[0] if unschedulable_vm is not None else None

    # Counts are per VM: a rejection only confirms anything when the same VM was rejected on
    # every check, so a VM that is no longer rejected (or is gone) starts over.
    for vm_name in [name for name in hits_by_vm if name != observed_vm_name]:
        del hits_by_vm[vm_name]

    if unschedulable_vm is None:
        return

    vm_name, scheduler_message = unschedulable_vm
    hits = hits_by_vm.get(vm_name, 0) + 1
    hits_by_vm[vm_name] = hits
    LOGGER.warning(
        f"Migrated VM '{vm_name}' is unschedulable on the destination cluster "
        f"(check {hits}/{_UNSCHEDULABLE_CONFIRMATIONS_REQUIRED}): {scheduler_message}"
    )
    if hits >= _UNSCHEDULABLE_CONFIRMATIONS_REQUIRED:
        _dump_plan_storage_state(plan=plan, reason=f"vm-unschedulable-{vm_name}")
        raise MigrationVmUnschedulableError(vm_name=vm_name, scheduler_message=scheduler_message)


def _plan_conversions_yaml(plan: Plan) -> str:
    """Read the Conversion CRs the Plan owns and render them as YAML.

    Args:
        plan (Plan): Plan whose Conversion CRs are read.

    Returns:
        str: YAML of the plan-owned Conversion CRs, empty when there are none or the read failed.
    """
    try:
        conversions = get_plan_conversion_crs(plan_resource=plan)
        if not conversions:
            return ""
        return yaml.safe_dump([conversion.instance.to_dict() for conversion in conversions], sort_keys=False)
    except (
        ApiException,
        DynamicApiError,
        AttributeError,
        MaxRetryError,
        OSError,
        ProtocolError,
        TypeError,
        ValueError,
        yaml.YAMLError,
    ) as err:
        LOGGER.info(f"Could not read the Conversion CRs of plan '{plan.name}', skipping them: {err}")
        return ""


def _plan_pod_label_selector(plan: Plan) -> str:
    """Return the label selector matching the plan's own conversion pods.

    Args:
        plan (Plan): Plan whose migration pods are selected.

    Returns:
        str: Selector scoped to the plan's Migration, or the shared conversion-pod label when the
            Migration CR is already gone.
    """
    try:
        migration_uid = get_migration_for_plan(plan=plan).instance.metadata.uid
        return f"{CONVERSION_POD_LABEL_SELECTOR},migration={migration_uid}"
    except (
        MigrationNotFoundError,
        ApiException,
        DynamicApiError,
        AttributeError,
        MaxRetryError,
        OSError,
        ProtocolError,
        TypeError,
        ValueError,
    ) as err:
        LOGGER.info(f"Could not resolve the Migration of plan '{plan.name}', selecting every conversion pod: {err}")
        return CONVERSION_POD_LABEL_SELECTOR


def _run_evidence_command(*, command: list[str], deadline: float) -> str | None:
    """Run one evidence command, losing only its own artifact when it fails.

    Args:
        command (list[str]): The ``oc`` command to run.
        deadline (float): ``monotonic()`` value after which no further command is started.

    Returns:
        str | None: The command output, or None when the budget is spent or the command failed.
    """
    remaining = deadline - monotonic()
    if remaining <= 0:
        LOGGER.warning(f"Skipping the failure evidence command '{' '.join(command)}': the capture budget is spent")
        return None
    try:
        # check=False: a nonzero exit is a lost artifact, not an exception that could replace the
        # MigrationPlanExecError the caller raises.
        success, output, error = run_command(
            command,
            check=False,
            verify_stderr=False,
            timeout=min(_FAILURE_EVIDENCE_COMMAND_TIMEOUT_SECONDS, remaining),
        )
    except (TimeoutExpired, OSError) as err:
        LOGGER.warning(f"Failure evidence command '{' '.join(command)}' failed: {err}")
        return None
    if not success:
        LOGGER.warning(f"Failure evidence command '{' '.join(command)}' failed: {error}")
        return None
    return output


def _write_failure_evidence(*, path: Path, text: str, cluster_password: str | None) -> None:
    """Write one evidence artifact with the cluster password redacted.

    Args:
        path (Path): Destination file.
        text (str): Command output or Conversion dump about to be written.
        cluster_password (str | None): Configured cluster password, redacted when it is set.
    """
    if cluster_password:
        text = redact_secret(text=text, secret=cluster_password)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as err:
        LOGGER.warning(f"Failed to write the failure evidence file '{path}': {err}")


def _capture_failed_plan_evidence(plan: Plan, *, conversions_snapshot: str = "") -> None:
    """Capture the Conversion CRs, pipeline pod logs and namespace events of a failed plan.

    MTV deletes the failed objects on failure, so the forklift ``Conversion`` CR and its
    pipeline pods are read here, on the poll that reports the plan as Failed; a Conversion dump
    kept from an earlier poll is written when the CR is already gone. Capture is best effort: it
    never raises, because a lost artifact must not replace the MigrationPlanExecError the caller
    raises.

    Args:
        plan (Plan): Plan whose migration status was seen as Failed.
        conversions_snapshot (str): Non-empty Conversion dump taken while the plan was running,
            used when the CR no longer exists.
    """
    # Resolved before the namespace so --skip-data-collector costs no API request or oc call.
    collector_path = data_collector_path()
    if collector_path is None:
        return
    namespace = _plan_target_namespace(plan=plan)
    if namespace is None:
        return

    directory = collector_path / f"{_FAILURE_EVIDENCE_DIR_PREFIX}-{plan.name}-{utc_timestamp()}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        LOGGER.warning(f"Failed to create the failure evidence directory '{directory}': {err}")
        return

    deadline = monotonic() + _FAILURE_EVIDENCE_TOTAL_TIMEOUT_SECONDS
    cluster_password = resolve_cluster_credential("cluster_password")
    pod_selector = _plan_pod_label_selector(plan=plan)

    _write_failure_evidence(
        path=directory / "conversions.yaml",
        text=_plan_conversions_yaml(plan=plan) or conversions_snapshot or _FAILURE_EVIDENCE_NO_CONVERSIONS_MARKER,
        cluster_password=cluster_password,
    )

    pods_yaml = _run_evidence_command(
        command=["oc", "get", "pods", "-n", namespace, "-l", pod_selector, "-o", "yaml"],
        deadline=deadline,
    )
    if pods_yaml is not None:
        _write_failure_evidence(path=directory / "pods.yaml", text=pods_yaml, cluster_password=cluster_password)

    # Namespace-wide events are kept: the reason a pod never started is often only there.
    events_yaml = _run_evidence_command(
        command=["oc", "get", "events", "-n", namespace, "-o", "yaml"], deadline=deadline
    )
    if events_yaml is not None:
        _write_failure_evidence(path=directory / "events.yaml", text=events_yaml, cluster_password=cluster_password)

    pod_names = _run_evidence_command(
        command=["oc", "get", "pods", "-n", namespace, "-l", pod_selector, "-o", "name"],
        deadline=deadline,
    )
    for pod_name in (pod_names or "").split():
        if not any(marker in pod_name for marker in _FAILURE_EVIDENCE_POD_MARKERS):
            continue
        logs = _run_evidence_command(
            command=["oc", "logs", "-n", namespace, pod_name, f"--tail={_FAILURE_EVIDENCE_LOG_TAIL_LINES}"],
            deadline=deadline,
        )
        if logs is not None:
            _write_failure_evidence(
                path=directory / f"{pod_name.replace('/', '-')}.log",
                text=logs,
                cluster_password=cluster_password,
            )


def wait_for_migration_complate(
    plan: Plan,
    *,
    on_status_poll: Callable[[str], None] | None = None,
) -> None:
    """Wait for migration to complete.

    Args:
        plan (Plan): The Plan resource to monitor
        on_status_poll (Callable[[str], None] | None): Optional callback invoked on each poll
            with the current migration status string

    Raises:
        MigrationPlanExecError: If migration fails or doesn't reach expected condition within timeout
        MigrationVmUnschedulableError: If a migrated VM is confirmed unschedulable by the destination scheduler.
        ValueError: If the Plan reports a malformed migration status.
    """
    next_unschedulable_check = monotonic()
    next_evidence_snapshot = monotonic()
    unschedulable_hits: dict[str, int] = {}
    conversions_snapshot = ""

    try:
        last_status: str = ""

        for sample in TimeoutSampler(
            func=get_plan_migration_status,
            sleep=1,
            wait_timeout=py_config["plan_wait_timeout"],
            plan=plan,
        ):
            if sample != last_status:
                LOGGER.info(f"Plan '{plan.name}' migration status: '{sample}'")
                last_status = sample

            if on_status_poll is not None:
                on_status_poll(sample)

            if sample == Plan.Status.SUCCEEDED:
                return

            elif sample == Plan.Status.FAILED:
                _capture_failed_plan_evidence(plan=plan, conversions_snapshot=conversions_snapshot)
                raise MigrationPlanExecError()

            # Sampled while the plan runs: MTV deletes the Conversion CR before the Plan reports
            # Failed, so the last non-empty dump is the only reason the bundle can still carry.
            if data_collector_path() is not None and monotonic() >= next_evidence_snapshot:
                next_evidence_snapshot = monotonic() + _FAILURE_EVIDENCE_SNAPSHOT_INTERVAL_SECONDS
                conversions_snapshot = _plan_conversions_yaml(plan=plan) or conversions_snapshot

            # Probed only on non-terminal polls, so a plan that reports its own failure on the
            # poll where a VM is confirmed unschedulable still surfaces the plan status.
            if monotonic() >= next_unschedulable_check:
                next_unschedulable_check = monotonic() + _UNSCHEDULABLE_CHECK_INTERVAL_SECONDS
                _confirm_unschedulable_destination_vm(plan=plan, hits_by_vm=unschedulable_hits)

    except (TimeoutExpiredError, MigrationPlanExecError) as err:
        # Storage evidence is collected for a wait timeout only: expected-failure tests raise the
        # MigrationPlanExecError below on purpose and must not pay for a dump.
        if isinstance(err, TimeoutExpiredError):
            _dump_plan_storage_state(plan=plan, reason="plan-wait-timeout")
        raise MigrationPlanExecError(
            f"Plan {plan.name} failed to reach the expected condition. \nstatus:\n\t{plan.instance}"
        )


def wait_for_dual_migration_completion(
    plan_1: Plan,
    plan_2: Plan,
    callback_1: Callable[[str], None],
    callback_2: Callable[[str], None],
) -> None:
    """Poll two migration plans until both complete, invoking callbacks on each poll.

    Args:
        plan_1 (Plan): First migration plan to monitor.
        plan_2 (Plan): Second migration plan to monitor.
        callback_1 (Callable[[str], None]): Status callback for plan 1.
        callback_2 (Callable[[str], None]): Status callback for plan 2.

    Raises:
        MigrationPlanExecError: If either plan fails or timeout expires before both complete.
    """
    plans = [plan_1, plan_2]
    callbacks = {plan_1.name: callback_1, plan_2.name: callback_2}

    LOGGER.info(f"Waiting for {len(plans)} migrations to complete")
    completed_plans: set[str] = set()
    last_statuses: dict[str, str] = {plan.name: "" for plan in plans}

    try:
        for _ in TimeoutSampler(
            func=lambda: None,
            sleep=1,
            wait_timeout=py_config["plan_wait_timeout"],
        ):
            for plan in plans:
                # Skip if already completed
                if plan.name in completed_plans:
                    continue

                # Fetch and log status on change
                status = get_plan_migration_status(plan=plan)
                if status != last_statuses[plan.name]:
                    LOGGER.info(f"Plan '{plan.name}' migration status: '{status}'")
                    last_statuses[plan.name] = status

                # Invoke callback for this plan
                callbacks[plan.name](status)

                # Handle terminal states
                if status == Plan.Status.SUCCEEDED:
                    completed_plans.add(plan.name)
                    LOGGER.info(f"Migration '{plan.name}' completed")
                elif status == Plan.Status.FAILED:
                    raise MigrationPlanExecError(f"Plan {plan.name} failed. \nstatus:\n\t{plan.instance}")

            # Break when all plans have completed
            if len(completed_plans) == len(plans):
                break

    except TimeoutExpiredError as timeout_err:
        expected_plans = {plan.name for plan in plans}
        raise MigrationPlanExecError(
            f"One or more migrations failed to complete within timeout. "
            f"Completed: {completed_plans}, Expected: {expected_plans}"
        ) from timeout_err


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
    per_nic_network_map: bool = False,
    network_map_overrides: list[dict[str, Any]] | None = None,
) -> NetworkMap:
    """Create a NetworkMap resource for migration.

    Args:
        fixture_store (dict[str, Any]): Fixture store for resource tracking.
        source_provider (BaseProvider): Source provider instance.
        destination_provider (BaseProvider): Destination provider instance.
        multus_network_name (dict[str, str]): Multus network name and namespace.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Target namespace.
        source_provider_inventory (ForkliftInventory): Source provider inventory.
        vms (list[str]): VM names to query.
        per_nic_network_map (bool): Create one map entry per NIC without deduplication.
        network_map_overrides (list[dict[str, Any]] | None): Explicit per-network
            destination overrides; each entry maps one source network to a pod,
            multus or ignored destination and may set a guest IP mode. When set,
            this replaces the automatic destination assignment.

    Returns:
        NetworkMap: The created NetworkMap resource.

    Raises:
        ValueError: If provider resources are not set, or an override is invalid.
    """
    if not source_provider.ocp_resource:
        raise ValueError("source_provider.ocp_resource is not set")

    if not destination_provider.ocp_resource:
        raise ValueError("destination_provider.ocp_resource is not set")

    network_map_list = gen_network_map_list(
        source_provider=source_provider,
        target_namespace=target_namespace,
        source_provider_inventory=source_provider_inventory,
        multus_network_name=multus_network_name,
        vms=vms,
        per_nic_network_map=per_nic_network_map,
        network_map_overrides=network_map_overrides,
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


def verify_vm_disk_count(
    destination_provider: OCPProvider,
    plan: dict[str, Any],
    target_namespace: str,
    additional_disks: int = 0,
) -> None:
    """Verify migrated VM disk counts against plan and fixture-attached disks.

    Args:
        destination_provider (OCPProvider): Provider for the destination cluster.
        plan (dict[str, Any]): Test plan dictionary containing VM configuration.
        target_namespace (str): Namespace where the VM was migrated.
        additional_disks (int): Number of disks attached outside the plan configuration.
    """
    LOGGER.info("Verifying disks on migrated VM in OpenShift.")
    for vm_config in plan["virtual_machines"]:
        source_vm_name = vm_config["name"]

        num_added_disks = len(vm_config.get("add_disks", []))
        expected_disks = 1 + num_added_disks + additional_disks

        LOGGER.info(f"Fetching details for migrated VM: {source_vm_name} in namespace {target_namespace}")
        migrated_vm_info = destination_provider.vm_dict(name=source_vm_name, namespace=target_namespace)
        num_disks_migrated = len(migrated_vm_info.get("disks", []))
        LOGGER.info(f"Found {num_disks_migrated} disks on migrated VM '{source_vm_name}'. Expecting {expected_disks}.")

        assert num_disks_migrated == expected_disks, (
            f"Expected {expected_disks} disks on migrated VM '{source_vm_name}', but found {num_disks_migrated}."
        )
        LOGGER.info(f"Successfully verified {expected_disks} disks on the migrated VM '{source_vm_name}'.")


def wait_for_concurrent_migration_execution(
    plan_list: list[Plan],
    timeout: int = 120,
    callbacks: dict[str, Callable[[str], None]] | None = None,
) -> None:
    """Wait for multiple migration plans to be executing simultaneously.

    Polls the status of all provided plans and validates that they all reach the "Executing"
    state at the same time. If any plan completes (Succeeded or Failed) before all plans
    are executing, the validation fails.

    Args:
        plan_list: List of Plan resources to monitor.
        timeout: Timeout in seconds to wait for simultaneous execution.
        callbacks: Optional dict mapping plan names to status poll callbacks. Callbacks are
            invoked with the current status string for their respective plan on each poll.

    Raises:
        AssertionError: If plans do not execute simultaneously or if any plan completes early.
    """
    LOGGER.info(f"Validating {len(plan_list)} migrations enter executing state simultaneously")
    plans_executing = {plan.name: False for plan in plan_list}
    callbacks = callbacks or {}

    try:
        for current_statuses in TimeoutSampler(
            func=lambda: {plan.name: get_plan_migration_status(plan) for plan in plan_list},
            sleep=2,
            wait_timeout=timeout,
        ):
            # Update executing state for each plan and invoke callbacks
            for plan in plan_list:
                status = current_statuses[plan.name]

                # Invoke callback if provided
                if plan.name in callbacks:
                    callbacks[plan.name](status)

                if status == Plan.Status.EXECUTING:
                    if not plans_executing[plan.name]:
                        LOGGER.info(f"Plan '{plan.name}' is now executing")
                    plans_executing[plan.name] = True
                    continue

                # Check for early completion failure
                elif status in (Plan.Status.SUCCEEDED, Plan.Status.FAILED):
                    # Construct error message with status of all plans
                    status_msg = ", ".join([f"{name}: {stat}" for name, stat in current_statuses.items()])
                    raise AssertionError(
                        f"Plan {plan.name} reached {status} before all plans were executing simultaneously. "
                        f"Statuses: {status_msg}"
                    )

            # Check if all plans are executing
            if all(plans_executing.values()):
                LOGGER.info("SUCCESS: All migrations are executing simultaneously")
                return

    except TimeoutExpiredError:
        raise AssertionError("Failed to validate all migrations executing simultaneously within timeout") from None
