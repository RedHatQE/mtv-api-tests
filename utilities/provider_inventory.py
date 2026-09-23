import time
from collections.abc import Callable
from typing import Any

from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory, VsphereForkliftInventory
from utilities.copyoffload_datastore import resolve_datastore_moid_from_disk_config

LOGGER = get_logger(__name__)

_INVENTORY_REFRESH_READY_TIMEOUT = 180
INVENTORY_SYNC_WORKAROUND_JIRA = "MTV-6072"
# Short initial wait before triggering a provider refresh — avoids refresh overhead
# when inventory syncs quickly on its own.
_QUICK_CHECK_TIMEOUT = 30


def force_inventory_refresh(provider: Provider) -> None:
    """Force Forklift provider inventory refresh by patching spec.settings._refresh.

    Waits for Ready (not Validated) because _refresh triggers reconciliation; Ready
    is sufficient for inventory repopulation to start.

    Args:
        provider (Provider): Forklift Provider resource to refresh.

    Raises:
        TimeoutExpiredError: If the provider does not become Ready within the timeout.
    """
    refresh_timestamp = str(int(time.time()))
    LOGGER.info(f"Forcing inventory refresh for provider '{provider.name}' with _refresh={refresh_timestamp}")
    patch = {"spec": {"settings": {"_refresh": refresh_timestamp}}}
    ResourceEditor(patches={provider: patch}).update()
    provider.wait_for_condition(condition="Ready", status="True", timeout=_INVENTORY_REFRESH_READY_TIMEOUT)


def collect_cross_datastore_ids(
    virtual_machines: list[dict[str, Any]],
    copyoffload_config: dict[str, Any],
) -> list[str]:
    """Collect MoIDs of datastores required for cross-datastore add_disks configurations.

    Args:
        virtual_machines: VM configurations from the test plan
        copyoffload_config: copyoffload section from source provider data

    Returns:
        Deduplicated list of datastore MoIDs to wait for in Forklift inventory

    Raises:
        ValueError: If a symbolic datastore key cannot be resolved from copyoffload config
    """
    datastore_ids: list[str] = []
    has_cross_datastore_disks = False

    for vm in virtual_machines:
        for disk in vm.get("add_disks", []):
            disk_datastore_id: str | None = disk.get("datastore_id")
            if not disk_datastore_id:
                continue

            has_cross_datastore_disks = True
            datastore_ids.append(
                resolve_datastore_moid_from_disk_config(
                    disk_datastore_id=disk_datastore_id,
                    copyoffload_config=copyoffload_config,
                )
            )

    if has_cross_datastore_disks:
        primary_datastore_id = copyoffload_config.get("datastore_id")
        if primary_datastore_id:
            datastore_ids.append(primary_datastore_id)

    return list(dict.fromkeys(datastore_ids))


def _wait_for_vsphere_host_and_datastore_inventory(
    source_provider_inventory: VsphereForkliftInventory,
    virtual_machines: list[dict[str, Any]],
    copyoffload_config: dict[str, Any],
    inventory_timeout: int,
) -> None:
    """Wait for vSphere host and cross-datastore inventory to sync (MTV-6066 workarounds 1+2).

    Args:
        source_provider_inventory: vSphere Forklift inventory client
        virtual_machines: VM configurations from the test plan
        copyoffload_config: copyoffload section from source provider data
        inventory_timeout: Maximum time to wait in seconds
    """
    source_provider_inventory.wait_for_hosts(timeout=inventory_timeout)

    required_datastore_ids = collect_cross_datastore_ids(
        virtual_machines=virtual_machines,
        copyoffload_config=copyoffload_config,
    )
    if required_datastore_ids:
        source_provider_inventory.wait_for_datastores(
            datastore_ids=required_datastore_ids,
            timeout=inventory_timeout,
        )


def wait_for_cloned_vms_in_forklift_inventory(
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    cloned_vm_names: list[str],
    virtual_machines: list[dict[str, Any]],
    copyoffload_config: dict[str, Any],
    inventory_timeout: int,
    jira_issue_open: Callable[[str], bool | None],
) -> None:
    """Wait for cloned VMs in Forklift inventory with MTV-6066 workarounds gated by MTV-6072.

    For vSphere when MTV-6072 is open: first tries each VM for _QUICK_CHECK_TIMEOUT seconds.
    VMs that appear quickly are done with no patch overhead. Only VMs that fail the quick check
    trigger a provider refresh + host/datastore inventory wait + full retry. When MTV-6072 is
    resolved, ``workaround_active`` becomes False and the plain wait path runs for all providers.

    ``jira_issue_open(...) is not False`` keeps workarounds active when Jira is unavailable
    (returns None). This is an intentional fail-safe: workarounds stay enabled unless Jira
    explicitly reports the issue as resolved.

    Args:
        source_provider: Source provider instance
        source_provider_inventory: Forklift inventory for the source provider
        cloned_vm_names: Names of cloned VMs to wait for
        virtual_machines: VM configurations from the test plan
        copyoffload_config: copyoffload section from source provider data
        inventory_timeout: Maximum time to wait in seconds
        jira_issue_open: Callable returning True if issue is open, False if resolved, None if unavailable

    Raises:
        TypeError: If vSphere provider inventory is not VsphereForkliftInventory
        ValueError: If source_provider.ocp_resource is not set when workaround is active
        TimeoutExpiredError: If a VM does not appear in inventory within the timeout
    """
    workaround_active = jira_issue_open(INVENTORY_SYNC_WORKAROUND_JIRA) is not False
    is_vsphere = source_provider.type == Provider.ProviderType.VSPHERE
    vsphere_inventory: VsphereForkliftInventory | None = None

    if is_vsphere and workaround_active:
        if not isinstance(source_provider_inventory, VsphereForkliftInventory):
            raise TypeError(
                f"vSphere provider requires VsphereForkliftInventory, got {type(source_provider_inventory).__name__}"
            )
        vsphere_inventory = source_provider_inventory

        # Quick check: try each VM for _QUICK_CHECK_TIMEOUT seconds first.
        # Avoids the 180s force_inventory_refresh overhead when VMs sync quickly on their own.
        failed_vm_names: list[str] = []
        for vm_name in cloned_vm_names:
            try:
                source_provider_inventory.wait_for_vm(name=vm_name, timeout=_QUICK_CHECK_TIMEOUT)
            except TimeoutExpiredError:
                failed_vm_names.append(vm_name)

        if failed_vm_names:
            # VMs not found in quick check — force refresh + prerequisites + full retry
            if source_provider.ocp_resource is None:
                raise ValueError("source_provider.ocp_resource is not set")
            LOGGER.info(
                f"Quick inventory check timed out for {failed_vm_names}; forcing provider refresh (MTV-6072 workaround)"
            )
            force_inventory_refresh(source_provider.ocp_resource)
            _wait_for_vsphere_host_and_datastore_inventory(
                source_provider_inventory=vsphere_inventory,
                virtual_machines=virtual_machines,
                copyoffload_config=copyoffload_config,
                inventory_timeout=inventory_timeout,
            )
            for vm_name in failed_vm_names:
                source_provider_inventory.wait_for_vm(name=vm_name, timeout=inventory_timeout)

        return

    # Non-vSphere or workaround inactive — plain wait per VM
    for vm_name in cloned_vm_names:
        source_provider_inventory.wait_for_vm(name=vm_name, timeout=inventory_timeout)
