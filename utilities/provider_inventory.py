import os
import time
import warnings
from time import monotonic
from typing import Any

from ocp_resources.provider import Provider
from ocp_resources.resource import ResourceEditor
from ovirtsdk4 import NotFoundError as OvirtNotFoundError
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

from exceptions.exceptions import VmNotFoundError
from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from libs.providers.rhv import OvirtProvider

LOGGER = get_logger(__name__)

_INVENTORY_REFRESH_READY_TIMEOUT = 180
# The refresh triggered from the convergence path must not consume the whole convergence window.
_CONVERGENCE_REFRESH_READY_TIMEOUT = 60
# Timeout for inventory NIC MACs to converge with the live provider (see
# _wait_for_inventory_mac_convergence). The inventory is an eventually-consistent cache: an
# already-inventoried clone has been observed serving its pre-clone MACs for more than 180s
# (CI runs #22/#25/#27 on both vCenters), so the wait refreshes on demand instead of assuming
# the cache updates itself within any particular interval.
_MAC_CONVERGENCE_TIMEOUT = 180
_MAC_CONVERGENCE_SLEEP = 5
# A single inventory read is retried for this long before it counts as "not converged": a
# transient API interruption must not abort clone setup, which is what the sampler-based check
# used to guarantee.
_INVENTORY_READ_RETRY_TIMEOUT = 30
# Refresh offsets (seconds from the start of the wait). Triggering a refresh up front, and again
# inside the window, means a refresh lost to a race (collector snapshot vs. controller reconcile)
# is retried instead of leaving the caller to poll an inventory that was never refreshed.
_MAC_CONVERGENCE_REFRESH_OFFSETS = (0, 60, 120)


def wait_for_inventory_guest_networks(
    source_provider_inventory: ForkliftInventory,
    vm_names: list[str],
    source_vms_data: dict[str, dict[str, Any]],
) -> None:
    """Wait for inventory IPv4 readiness and the saved source static addresses.

    Require a gateway if the saved source snapshot reports one or has incomplete
    network metadata. Waive it only when interfaces and their IP entries are all
    present and every IP entry explicitly reports a falsy gateway. Also require
    each saved static IPv4 address on its matching inventory entry, with a gateway
    when that source IP entry reports one or omits the gateway key. This check does
    not establish source snapshot freshness.

    Args:
        source_provider_inventory (ForkliftInventory): Source provider inventory.
        vm_names (list[str]): VM names to poll.
        source_vms_data (dict[str, dict[str, Any]]): Saved normalized provider VM
            snapshots keyed by VM name (plan["source_vms_data"]), containing
            heterogeneous provider fields. Incomplete network metadata keeps the
            gateway requirement; explicit gatewayless metadata waives it.

    Raises:
        TimeoutExpiredError: If a VM lacks a baseline inventory IPv4 entry, an
            expected static address, or a gateway on an entry that requires one,
            after the timeout.
        ValueError: If HYPERV_REFRESH_INTERVAL is not a positive integer.
    """
    interval_raw = os.environ.get("HYPERV_REFRESH_INTERVAL", "10")
    try:
        interval = int(interval_raw)
    except ValueError:
        raise ValueError(f"HYPERV_REFRESH_INTERVAL must be a positive integer, got '{interval_raw}'") from None
    if interval <= 0:
        raise ValueError(f"HYPERV_REFRESH_INTERVAL must be a positive integer, got {interval}")
    timeout = 10 * (interval + 30) + 300

    def _ready() -> bool:
        """Check baseline readiness and each expected static address.

        Returns:
            bool: Whether every VM satisfies both inventory readiness checks.
        """
        for name in vm_names:
            try:
                networks = source_provider_inventory.get_vm(name=name).get("guestNetworks") or []
            except ValueError:
                return False
            source_vm_data = source_vms_data[name]
            require_gateway = _source_vm_requires_gateway(source_vm_data=source_vm_data)
            if not any(
                isinstance(entry, dict)
                and entry.get("ip")
                and ":" not in str(entry["ip"])
                and (entry.get("gateway") or not require_gateway)
                for entry in networks
            ):
                return False
            if not all(
                any(
                    isinstance(entry, dict)
                    and entry.get("ip") == ip_config["ip_address"]
                    and (entry.get("gateway") or not _source_ip_requires_gateway(ip_config=ip_config))
                    for entry in networks
                )
                for interface in source_vm_data.get("network_interfaces") or []
                for ip_config in interface.get("ip_addresses") or []
                if ip_config.get("is_static_ip") is True
            ):
                return False
        return True

    for ready in TimeoutSampler(wait_timeout=timeout, sleep=10, func=_ready):
        if ready:
            return


def _source_ip_requires_gateway(ip_config: dict[str, Any]) -> bool:
    """Require an inventory gateway unless the source IP explicitly has none.

    Args:
        ip_config (dict[str, Any]): Normalized source IP metadata containing
            heterogeneous external provider values.

    Returns:
        bool: True when the gateway key is omitted or its value is truthy.
    """
    return "gateway" not in ip_config or bool(ip_config["gateway"])


def _source_vm_requires_gateway(source_vm_data: dict[str, Any]) -> bool:
    """Keep the inventory gateway requirement unless the source is explicitly gatewayless.

    Missing or empty interfaces or IP entries, omitted gateway keys, and truthy
    gateways all require an inventory gateway.

    Args:
        source_vm_data (dict[str, Any]): Saved normalized provider VM snapshot
            (plan["source_vms_data"] entry), containing heterogeneous provider fields.

    Returns:
        bool: False only for non-empty source interfaces whose IP entries are all
            present and explicitly report falsy gateways; True otherwise.
    """
    interfaces: list[dict[str, Any]] = source_vm_data.get("network_interfaces") or []
    if not interfaces:
        return True
    for interface in interfaces:
        ip_addresses: list[dict[str, Any]] = interface.get("ip_addresses") or []
        if not ip_addresses:
            return True
        for ip_config in ip_addresses:
            if _source_ip_requires_gateway(ip_config=ip_config):
                return True
    return False


def _next_refresh_timestamp(provider: Provider) -> str:
    """Return an ``_refresh`` value strictly greater than the provider's current one.

    ``_refresh`` is an opaque trigger the controller reconciles on: writing the same value again
    is a no-op, and the value is second-granular, so two refreshes within one second must still
    differ. The provider's own settings are the source of truth, which keeps the sequence
    monotonic per provider without module-level state.

    Args:
        provider (Provider): Forklift Provider resource about to be patched.

    Returns:
        str: The next refresh trigger value.
    """
    current = str((provider.instance.to_dict().get("spec", {}).get("settings") or {}).get("_refresh", ""))
    now = int(time.time())
    if current.isdigit() and int(current) >= now:
        return str(int(current) + 1)
    return str(now)


def force_inventory_refresh(
    provider: Provider,
    ready_timeout: int = _INVENTORY_REFRESH_READY_TIMEOUT,
) -> None:
    """Force Forklift provider inventory refresh by patching spec.settings._refresh.

    The written value is always different from the provider's current one, so consecutive calls
    in the same second still trigger a reconcile. Waits for Ready (not Validated) because
    _refresh triggers reconciliation; Ready is sufficient for inventory repopulation to start.

    Args:
        provider (Provider): Forklift Provider resource to refresh.
        ready_timeout (int): Seconds to wait for the provider Ready condition.

    Raises:
        TimeoutExpiredError: If the provider does not become Ready within the timeout.
    """
    refresh_timestamp = _next_refresh_timestamp(provider=provider)
    LOGGER.info(f"Forcing inventory refresh for provider '{provider.name}' with _refresh={refresh_timestamp}")
    patch = {"spec": {"settings": {"_refresh": refresh_timestamp}}}
    ResourceEditor(patches={provider: patch}).update()
    provider.wait_for_condition(condition="Ready", status="True", timeout=ready_timeout)


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


def wait_for_shared_rdm_disk_in_forklift_inventory(
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    vm_names: list[str],
    timeout: int = _INVENTORY_REFRESH_READY_TIMEOUT,
    sleep: int = 10,
) -> None:
    """Force a provider refresh and wait until each VM's RDM disk is reported as shared.

    Linking two VMs to the same RDM mapping file (``VMWareProvider.add_shared_rdm_disk``) is a
    live vCenter reconfiguration performed after the VMs already synced to Forklift inventory, so
    inventory can still report the RDM disk as unshared. Building the Plan from that stale
    inventory would compute ``shouldSCSIReservation()`` as false regardless of the configured
    ``scsiReservation`` setting. This forces a refresh and blocks until each VM's inventory RDM
    disk (``rdm: true``) reports ``shared: true``.

    Args:
        source_provider: Source provider instance (must have ocp_resource set).
        source_provider_inventory: Forklift inventory client for the source provider.
        vm_names: Names of the VMs sharing the RDM disk.
        timeout: Maximum time to wait for every VM's RDM disk to report shared.
        sleep: Seconds between inventory polls.

    Raises:
        ValueError: If source_provider.ocp_resource is not set.
        TimeoutExpiredError: If any VM's RDM disk does not report shared within the timeout.
    """
    if source_provider.ocp_resource is None:
        raise ValueError("source_provider.ocp_resource is not set")

    LOGGER.info(f"Forcing inventory refresh to sync shared RDM disk for VMs: {vm_names}")
    force_inventory_refresh(source_provider.ocp_resource)

    def _all_rdm_disks_shared() -> bool:
        """Return True once every VM's RDM disk reports shared: true.

        A transient VM absence (ValueError from get_vm) or a VM with no RDM disk yet counts as
        not-ready, letting TimeoutSampler keep retrying while the forced refresh reconciles.
        """
        for vm_name in vm_names:
            try:
                disks = source_provider_inventory.get_vm(name=vm_name).get("disks", [])
            except ValueError:
                LOGGER.info(f"VM '{vm_name}' not yet in inventory after refresh, retrying")
                return False
            rdm_disk = next((disk for disk in disks if disk.get("rdm")), None)
            if rdm_disk is None:
                LOGGER.info(f"VM '{vm_name}' has no RDM disk in inventory yet, retrying")
                return False
            if not rdm_disk.get("shared"):
                LOGGER.info(f"VM '{vm_name}' RDM disk not yet reported as shared, retrying")
                return False
        return True

    for is_shared in TimeoutSampler(wait_timeout=timeout, sleep=sleep, func=_all_rdm_disks_shared):
        if is_shared:
            LOGGER.info("Shared RDM disk present and marked shared in Forklift inventory for all VMs")
            return


def _wait_for_inventory_mac_convergence(
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    vm_names: list[str],
) -> None:
    """Wait until every cloned VM's inventory NIC MACs match live vCenter.

    The provider inventory is an eventually-consistent cache: a cloned VM can be listed with its
    pre-clone MACs for more than 180s (observed in CI runs #22/#25/#27 on both vCenters), so the
    wait refreshes the provider inventory on demand instead of assuming the cache updates itself.

    One refresh is shared by the whole clone batch and repeated at fixed offsets while any VM is
    still stale, so a refresh lost to a race (collector snapshot vs. controller reconcile) is
    retried without paying the provider-ready wait once per VM. Staleness is reported with a
    warning: a regression in inventory updates should be visible in the run summary rather than
    absorbed silently.

    Args:
        source_provider (BaseProvider): Live source provider, used for the live MAC values and the
            provider resource that receives the refresh trigger.
        source_provider_inventory (ForkliftInventory): Forklift inventory client for the provider.
        vm_names (list[str]): Names of the cloned VMs to check.

    Raises:
        ValueError: If no MACs are found in the live provider data, if the provider has no OCP
            resource to refresh, or if inventory and live MACs do not converge within the timeout.
    """
    live_macs_by_vm = {
        vm_name: _live_nic_macs(source_provider=source_provider, vm_name=vm_name) for vm_name in vm_names
    }
    for vm_name, live_macs in live_macs_by_vm.items():
        if not live_macs:
            raise ValueError(f"No MAC addresses found in live provider data for VM '{vm_name}'")

    LOGGER.info(f"Waiting for inventory NIC MACs to converge with live vCenter for {sorted(live_macs_by_vm)}")

    def _stale_vms() -> dict[str, tuple[list[str], list[str]]]:
        """Return the VMs whose inventory MACs differ from live vCenter, with both value sets."""
        stale: dict[str, tuple[list[str], list[str]]] = {}
        for vm_name, live_macs in live_macs_by_vm.items():
            inventory_macs = _inventory_nic_macs(
                source_provider_inventory=source_provider_inventory,
                vm_name=vm_name,
            )
            if inventory_macs != live_macs:
                stale[vm_name] = (sorted(inventory_macs), sorted(live_macs))
        return stale

    # Refresh first: polling a cache that is known to lag cannot converge on its own.
    stale_vms = _stale_vms()
    if not stale_vms:
        return
    if source_provider.ocp_resource is None:
        raise ValueError("source_provider.ocp_resource is not set")
    warnings.warn(
        f"Inventory NIC MACs are stale for cloned VM(s) {sorted(stale_vms)}; refreshing the provider "
        f"inventory on demand. Details (inventory vs live): {stale_vms}",
        stacklevel=2,
    )

    refresh_offsets = list(_MAC_CONVERGENCE_REFRESH_OFFSETS)
    started = monotonic()
    # Check first, then decide whether to wait: the last scheduled refresh is always followed by
    # another read, so an inventory update that lands during the refresh wait still converges.
    while True:
        stale_vms = _stale_vms()
        if not stale_vms:
            return
        remaining = _MAC_CONVERGENCE_TIMEOUT - (monotonic() - started)
        if remaining <= 0:
            break
        if refresh_offsets and monotonic() - started >= refresh_offsets[0]:
            refresh_offsets.pop(0)
            force_inventory_refresh(
                provider=source_provider.ocp_resource,
                # Never let the provider-ready wait consume the rest of the convergence window.
                ready_timeout=max(1, min(_CONVERGENCE_REFRESH_READY_TIMEOUT, int(remaining))),
            )
            continue
        time.sleep(min(_MAC_CONVERGENCE_SLEEP, remaining))

    raise ValueError(
        f"Inventory NIC MACs for cloned VM(s) {sorted(stale_vms)} did not converge with live vCenter "
        f"MACs after {_MAC_CONVERGENCE_TIMEOUT}s. Details (inventory vs live): {stale_vms}"
    )


def _live_nic_macs(source_provider: BaseProvider, vm_name: str) -> set[str]:
    """Return the VM's NIC MACs as reported by the live provider.

    Args:
        source_provider (BaseProvider): Live source provider.
        vm_name (str): VM name.

    Returns:
        set[str]: Lower-cased MAC addresses; empty when the provider reports none.
    """
    return {
        mac.lower()
        for nic in source_provider.vm_dict(name=vm_name).get("network_interfaces", [])
        if (mac := nic.get("macAddress"))
    }


def _inventory_nic_macs(
    source_provider_inventory: ForkliftInventory,
    vm_name: str,
) -> set[str]:
    """Return the VM's NIC MACs as currently reported by the provider inventory.

    A transient absence (ValueError from get_vm) and a present-but-null nics list both count as
    "no MACs yet" so polling continues while a refresh reconciles.

    Args:
        source_provider_inventory (ForkliftInventory): Forklift inventory client.
        vm_name (str): VM name.

    Returns:
        set[str]: Lower-cased MAC addresses; empty when the VM is not readable yet.
    """
    try:
        for vm in TimeoutSampler(
            wait_timeout=_INVENTORY_READ_RETRY_TIMEOUT,
            sleep=_MAC_CONVERGENCE_SLEEP,
            func=_read_inventory_vm,
            source_provider_inventory=source_provider_inventory,
            vm_name=vm_name,
        ):
            return {mac.lower() for nic in vm.get("nics") or [] if (mac := nic.get("mac"))}
    except TimeoutExpiredError:
        LOGGER.warning(
            f"Inventory read for VM '{vm_name}' kept failing for {_INVENTORY_READ_RETRY_TIMEOUT}s; "
            "treating its MACs as not yet converged"
        )
        return set()
    return set()


def _read_inventory_vm(
    source_provider_inventory: ForkliftInventory,
    vm_name: str,
) -> dict[str, Any]:
    """Read one VM from the provider inventory.

    A transient absence (``ValueError`` from ``get_vm``) is reported as an empty record so the
    surrounding sampler retries instead of failing the run.

    Args:
        source_provider_inventory (ForkliftInventory): Forklift inventory client.
        vm_name (str): VM name.

    Returns:
        dict[str, Any]: The VM record, or an empty dict when it is not listed yet.
    """
    try:
        return source_provider_inventory.get_vm(name=vm_name)
    except ValueError:
        return {}


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
        _wait_for_inventory_mac_convergence(
            source_provider=source_provider,
            source_provider_inventory=source_provider_inventory,
            vm_names=cloned_vm_names,
        )


def get_missing_source_vms(
    source_provider: BaseProvider,
    source_provider_inventory: ForkliftInventory,
    vm_names: list[str],
) -> list[str]:
    """Return the names from ``vm_names`` that MTV cannot resolve on the source provider.

    The Forklift inventory is what MTV actually migrates, so VM names are checked against
    its ``vms`` listing (one request, local membership check). RHV is the exception: RHV
    sources clone from templates, which the inventory does not list (see
    ``get_vm_or_template_networks``), so template names are verified on the provider.

    Args:
        source_provider (BaseProvider): Source provider instance.
        source_provider_inventory (ForkliftInventory): Forklift inventory of the source provider.
        vm_names (list[str]): VM names to check.

    Returns:
        list[str]: Names MTV cannot resolve on the source provider.
    """
    if isinstance(source_provider, OvirtProvider):
        missing: list[str] = []
        for name in vm_names:
            try:
                source_provider.get_template_by_name(name=name)
            except OvirtNotFoundError:
                missing.append(name)
        return missing

    inventory_vm_names = set(source_provider_inventory.vms_names)
    return [name for name in vm_names if name not in inventory_vm_names]


def raise_for_missing_source_vms(source_provider: BaseProvider, provider_name: str, missing: list[str]) -> None:
    """Raise ``VmNotFoundError`` listing source VMs missing from the source provider.

    Args:
        source_provider (BaseProvider): Source provider instance used for type and host context.
        provider_name (str): Configured name of the source provider (e.g. ``vsphere-8.0.1``).
        missing (list[str]): Missing VM names.

    Raises:
        VmNotFoundError: If ``missing`` is not empty.
    """
    if missing:
        raise VmNotFoundError(
            f"Source VMs not found on source provider '{provider_name}' "
            f"[{source_provider.host}] ({source_provider.type}): {', '.join(missing)}. "
            f"Verify names in the test config match VMs on the source provider."
        )
