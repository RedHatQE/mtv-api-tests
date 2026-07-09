import time
from collections.abc import Callable
from typing import Any

from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory, VsphereForkliftInventory

LOGGER = get_logger(__name__)

_INVENTORY_REFRESH_READY_TIMEOUT = 180
INVENTORY_SYNC_WORKAROUND_JIRA = "MTV-6072"


def force_inventory_refresh(provider: Provider) -> None:
    """Force Forklift provider inventory refresh by patching spec.settings._refresh.

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
            if disk_datastore_id == "secondary_datastore_id":
                resolved_id = copyoffload_config.get("secondary_datastore_id")
                if not resolved_id:
                    raise ValueError(
                        "Disk requested secondary datastore but copyoffload.secondary_datastore_id is not configured"
                    )
                datastore_ids.append(resolved_id)
            elif disk_datastore_id == "non_xcopy_datastore_id":
                resolved_id = copyoffload_config.get("non_xcopy_datastore_id")
                if not resolved_id:
                    raise ValueError(
                        "Disk requested non-XCOPY datastore but copyoffload.non_xcopy_datastore_id is not configured"
                    )
                datastore_ids.append(resolved_id)
            else:
                datastore_ids.append(disk_datastore_id)

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

    When MTV-6072 is open (or Jira is unavailable), vSphere providers run host/datastore
    inventory waits before VM validation and force a provider refresh on VM wait timeout.
    When MTV-6072 is resolved, only the plain wait_for_vm loop runs.

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
        TimeoutExpiredError: If a VM does not appear in inventory within the timeout
    """
    workaround_active = jira_issue_open(INVENTORY_SYNC_WORKAROUND_JIRA) is not False
    is_vsphere = source_provider.type == Provider.ProviderType.VSPHERE

    if is_vsphere and workaround_active:
        if not isinstance(source_provider_inventory, VsphereForkliftInventory):
            raise TypeError(
                f"vSphere provider requires VsphereForkliftInventory, got {type(source_provider_inventory).__name__}"
            )
        _wait_for_vsphere_host_and_datastore_inventory(
            source_provider_inventory=source_provider_inventory,
            virtual_machines=virtual_machines,
            copyoffload_config=copyoffload_config,
            inventory_timeout=inventory_timeout,
        )

    for vm_name in cloned_vm_names:
        try:
            source_provider_inventory.wait_for_vm(name=vm_name, timeout=inventory_timeout)
        except TimeoutExpiredError:
            if is_vsphere and workaround_active and source_provider.ocp_resource:
                force_inventory_refresh(source_provider.ocp_resource)
                if not isinstance(source_provider_inventory, VsphereForkliftInventory):
                    raise TypeError(
                        f"vSphere provider requires VsphereForkliftInventory, "
                        f"got {type(source_provider_inventory).__name__}"
                    )
                _wait_for_vsphere_host_and_datastore_inventory(
                    source_provider_inventory=source_provider_inventory,
                    virtual_machines=virtual_machines,
                    copyoffload_config=copyoffload_config,
                    inventory_timeout=inventory_timeout,
                )
                source_provider_inventory.wait_for_vm(name=vm_name, timeout=inventory_timeout)
            else:
                raise
