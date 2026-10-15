import os
import time
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
# Timeout for inventory MACs to converge with the live provider (see _wait_for_inventory_mac_convergence).
_MAC_CONVERGENCE_TIMEOUT = 60
_MAC_CONVERGENCE_SLEEP = 5


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
