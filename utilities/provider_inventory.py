import time

from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from ovirtsdk4 import NotFoundError as OvirtNotFoundError
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import VmNotFoundError
from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.openstack import OpenStackProvider
from libs.providers.rhv import OvirtProvider
from libs.providers.vmware import VMWareProvider

LOGGER = get_logger(__name__)

_INVENTORY_REFRESH_READY_TIMEOUT = 180
# Timeout for inventory MACs to converge with the live provider (see _wait_for_inventory_mac_convergence).
_MAC_CONVERGENCE_TIMEOUT = 60
_MAC_CONVERGENCE_SLEEP = 5


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


def wait_for_added_nics_in_forklift_inventory(
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    expected_nic_counts: dict[str, int],
    timeout: int = _INVENTORY_REFRESH_READY_TIMEOUT,
    sleep: int = 10,
) -> None:
    """Force a provider refresh and wait until added NICs appear in the Forklift inventory.

    ``add_nic`` reconfigures the cloned source VM's hardware after it was already synced to
    inventory. ``wait_for_cloned_vms_in_forklift_inventory`` only waits for the VM to exist, so
    inventory can still be missing the new NIC. The NetworkMap is then built from that stale
    inventory and the added NIC is never mapped, so Forklift drops it during VM creation. This
    forces a refresh and blocks until each VM's inventory ``nics`` count reaches the expected
    total, guaranteeing NetworkMap creation sees the added NIC.

    Args:
        source_provider: Source provider instance (must have ocp_resource set).
        source_provider_inventory: Forklift inventory client for the source provider.
        expected_nic_counts: Mapping of VM name -> expected total NIC count after add_nic.
        timeout: Maximum time to wait for every VM to reach its expected NIC count.
        sleep: Seconds between inventory polls.

    Returns:
        None. Returning normally means every VM reached its expected NIC count; otherwise
        TimeoutExpiredError is raised.

    Raises:
        ValueError: If source_provider.ocp_resource is not set.
        TimeoutExpiredError: If any VM does not reach its expected NIC count within the timeout.
    """
    if source_provider.ocp_resource is None:
        raise ValueError("source_provider.ocp_resource is not set")

    LOGGER.info(f"Forcing inventory refresh to sync added NICs for VMs: {list(expected_nic_counts)}")
    force_inventory_refresh(source_provider.ocp_resource)

    def _all_nics_present() -> bool:
        """Return True once every expected VM shows at least its expected NIC count.

        Returns False (not ready) as soon as any VM is missing NICs — or is temporarily
        absent from the inventory (get_vm() reports this by raising ValueError), or its
        inventory record has no usable ``nics`` list yet (the field is null or not a list
        while the refresh is still reconciling). Treating all of these as not-ready lets
        TimeoutSampler keep retrying while the forced refresh reconciles, instead of
        aborting the whole setup.

        Returns:
            True if every VM has reached its expected NIC count; False otherwise.
        """
        for vm_name, expected_count in expected_nic_counts.items():
            try:
                nics = source_provider_inventory.get_vm(name=vm_name).get("nics")
            except ValueError:
                LOGGER.info(f"VM '{vm_name}' not yet in inventory after refresh, retrying")
                return False
            if not isinstance(nics, list):
                LOGGER.info(f"VM '{vm_name}' inventory has no 'nics' list yet ({nics!r}), retrying")
                return False
            actual_count = len(nics)
            if actual_count < expected_count:
                LOGGER.info(f"VM '{vm_name}' inventory has {actual_count} NIC(s), waiting for {expected_count}")
                return False
        return True

    for is_present in TimeoutSampler(wait_timeout=timeout, sleep=sleep, func=_all_nics_present):
        if is_present:
            LOGGER.info("All added NICs present in Forklift inventory")
            return


def _wait_for_inventory_mac_convergence(
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    vm_name: str,
) -> None:
    """Wait until Forklift inventory NIC MACs match the live vCenter MACs.

    vSphere clones regenerate MACs; vCenter finalizes them almost immediately, but
    inventory can briefly lag, risking a stale destination NIC MAC if migrated too soon.

    Args:
        source_provider (BaseProvider): Live source provider used to read the current vCenter MAC.
        source_provider_inventory (ForkliftInventory): Forklift inventory client for the source provider.
        vm_name (str): Name of the cloned VM to check.

    Raises:
        ValueError: If no MACs are found in the live provider data, or if inventory and live
            MACs do not converge within the timeout.
    """
    live_macs = {
        mac.lower()
        for nic in source_provider.vm_dict(name=vm_name).get("network_interfaces", [])
        if (mac := nic.get("macAddress"))
    }
    if not live_macs:
        raise ValueError(f"No MAC addresses found in live provider data for VM '{vm_name}'")

    LOGGER.info(f"Waiting for inventory NIC MACs to converge with live vCenter for '{vm_name}'")

    last_inventory_macs: set[str] = set()

    def _check_mac_convergence() -> bool:
        """Return True once inventory NIC MACs match the live vCenter MACs.

        A transient VM absence (ValueError from get_vm) counts as not-yet-converged so
        polling continues; the last observed inventory MACs are kept for timeout diagnostics.
        """
        nonlocal last_inventory_macs
        try:
            vm = source_provider_inventory.get_vm(name=vm_name)
        except ValueError:
            return False
        last_inventory_macs = {mac.lower() for nic in vm.get("nics", []) if (mac := nic.get("mac"))}
        return last_inventory_macs == live_macs

    try:
        for converged in TimeoutSampler(
            wait_timeout=_MAC_CONVERGENCE_TIMEOUT,
            sleep=_MAC_CONVERGENCE_SLEEP,
            func=_check_mac_convergence,
        ):
            if converged:
                return
    except TimeoutExpiredError as timeout_error:
        raise ValueError(
            f"Inventory NIC MACs for VM '{vm_name}' did not converge with live vCenter MACs after "
            f"{_MAC_CONVERGENCE_TIMEOUT}s. Inventory MACs: {sorted(last_inventory_macs)}, "
            f"live vCenter MACs: {sorted(live_macs)}"
        ) from timeout_error


def wait_for_cloned_vms_in_forklift_inventory(
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    cloned_vm_names: list[str],
    inventory_timeout: int,
) -> None:
    """Wait for cloned VMs to appear in Forklift inventory.

    For vSphere, also waits for inventory NIC MACs to converge with live vCenter MACs
    before returning (see ``_wait_for_inventory_mac_convergence``).

    Args:
        source_provider: Source provider instance
        source_provider_inventory: Forklift inventory for the source provider
        cloned_vm_names: Names of cloned VMs to wait for
        inventory_timeout: Maximum time to wait in seconds

    Raises:
        ValueError: If inventory/live NIC MACs do not converge within the timeout
        TimeoutExpiredError: If a VM does not appear in inventory within the timeout
    """
    for vm_name in cloned_vm_names:
        source_provider_inventory.wait_for_vm(name=vm_name, timeout=inventory_timeout)

    if source_provider.type == Provider.ProviderType.VSPHERE:
        for vm_name in cloned_vm_names:
            _wait_for_inventory_mac_convergence(
                source_provider=source_provider,
                source_provider_inventory=source_provider_inventory,
                vm_name=vm_name,
            )


def validate_source_vms_exist(source_provider: BaseProvider, vm_names: list[str]) -> None:
    """Validate that all source VMs/templates exist on the provider before cloning.

    For RHV, validates template names (RHV clones from templates, not VMs).
    For VMware and OpenStack, validates VM names.

    Args:
        source_provider: Source provider instance (VMware, RHV, or OpenStack).
        vm_names: VM or template names to check on the source provider.

    Raises:
        VmNotFoundError: If one or more VMs/templates are not found on the source provider.
    """
    missing: list[str] = []

    if isinstance(source_provider, OvirtProvider):
        for name in vm_names:
            try:
                source_provider.get_template_by_name(name=name)
            except OvirtNotFoundError:
                missing.append(name)
        entity_type = "templates"
    elif isinstance(source_provider, (VMWareProvider, OpenStackProvider)):
        for name in vm_names:
            try:
                source_provider.get_vm_by_name(query=name)
            except VmNotFoundError:
                missing.append(name)
        entity_type = "VMs"
    else:
        raise TypeError(f"Unsupported provider type for VM validation: {type(source_provider).__name__}")

    if missing:
        raise VmNotFoundError(
            f"Source {entity_type} not found on {source_provider.type} provider [{source_provider.host}]: "
            f"{', '.join(missing)}. "
            f"Verify names in the test config match {entity_type} on the source provider."
        )
