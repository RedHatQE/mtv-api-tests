from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any

from kubernetes.dynamic.client import DynamicClient
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.provider import Provider
from ocp_resources.route import Route
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

if TYPE_CHECKING:
    from libs.base_provider import BaseProvider

PROVIDER_INVENTORY_MAP: dict[str, type[ForkliftInventory]] = {}

LOGGER = get_logger(__name__)


def _extract_storage_ids(storages: list[dict[str, Any]]) -> set[str]:
    """Extract datastore MoIDs from Forklift storage inventory entries.

    Args:
        storages: Storage dictionaries from Forklift inventory

    Returns:
        Set of datastore MoIDs present in the inventory
    """
    return {storage["id"] for storage in storages if storage.get("id")}


def _register_inventory_classes() -> None:
    """Populate PROVIDER_INVENTORY_MAP after all classes are defined."""
    PROVIDER_INVENTORY_MAP.update({
        Provider.ProviderType.OVA: OvaForkliftInventory,
        Provider.ProviderType.RHV: OvirtForkliftInventory,
        Provider.ProviderType.VSPHERE: VsphereForkliftInventory,
        Provider.ProviderType.OPENSHIFT: OpenshiftForkliftInventory,
        Provider.ProviderType.OPENSTACK: OpenstackForliftinventory,
    })


def create_forklift_inventory(
    client: DynamicClient,
    mtv_namespace: str,
    provider: BaseProvider,
) -> ForkliftInventory:
    """ForkliftInventory instance for the given provider.

    Args:
        client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): MTV operator namespace.
        provider (BaseProvider): Source provider instance.

    Returns:
        ForkliftInventory: Inventory instance matching the provider type.

    Raises:
        ValueError: If ocp_resource is not set on the provider.
        ValueError: If the provider type is not supported.
    """
    if not PROVIDER_INVENTORY_MAP:
        _register_inventory_classes()

    if provider.ocp_resource is None:
        raise ValueError(f"{provider.type} provider ocp_resource is not set")

    provider_class = PROVIDER_INVENTORY_MAP.get(provider.type)
    if provider_class is None:
        raise ValueError(f"Provider {provider.type} not implemented")

    return provider_class(  # type: ignore[call-arg]  # Subclasses use 'namespace' param while base class uses 'mtv_namespace'
        client=client,
        namespace=mtv_namespace,
        provider_name=provider.ocp_resource.name,
    )


class ForkliftInventory(abc.ABC):
    def __init__(self, client: DynamicClient, provider_name: str, mtv_namespace: str, provider_type: str) -> None:
        self.client = client
        self.route = Route(client=self.client, name="forklift-inventory", namespace=mtv_namespace)
        self.provider_name = provider_name
        self.provider_type = provider_type
        self.provider_id = self._provider_id
        self.provider_url_path = f"{self.provider_type}/{self.provider_id}"
        self.vms_path = f"{self.provider_url_path}/vms"

    def _request(self, url_path: str = "") -> Any:
        return self.route.api_request(
            method="GET",
            url=f"https://{self.route.host}",
            action=f"providers{f'/{url_path}' if url_path else ''}",
        )

    @property
    def _provider_id(self) -> str:
        """
        Get the provider ID from the inventory, retrying until it is found.
        """
        try:
            for sample in TimeoutSampler(
                wait_timeout=180,
                sleep=5,
                func=lambda: [
                    _provider["id"]
                    for _provider in self._request(url_path=self.provider_type)
                    if _provider["name"] == self.provider_name
                ],
            ):
                if sample:
                    return sample[0]
        except TimeoutExpiredError:
            LOGGER.error(f"Timed out waiting for provider {self.provider_name} to appear in inventory.")
            raise

        # Unreachable: TimeoutSampler raises TimeoutExpiredError on timeout, but mypy needs this in order to pass type checking
        raise ValueError(f"Provider {self.provider_name} not found in inventory after waiting.")

    def get_data(self) -> dict[str, Any]:
        return self._request(url_path=self.provider_url_path)

    @property
    def vms(self) -> list[dict[str, Any]]:
        return self._request(url_path=self.vms_path)

    def get_vm(self, name: str) -> dict[str, Any]:
        for _vm in self.vms:
            if _vm["name"] == name:
                return self._request(url_path=f"{self.vms_path}/{_vm['id']}")

        raise ValueError(f"VM {name} not found. Available VMs: {self.vms_names}")

    def _check_openstack_volumes_synced(self, vm: dict[str, Any], vm_name: str) -> bool:
        """Verify OpenStack VM's attached volumes are synced and queryable.

        Args:
            vm (dict[str, Any]): VM data from Forklift inventory
            vm_name (str): VM name for logging

        Returns:
            bool: True if all volumes synced, False if still waiting

        Raises:
            ValueError: If volume query fails with validation error
            ConnectionError: If unable to connect to inventory API
            TimeoutError: If volume query times out
        """
        attached_volumes = vm.get("attachedVolumes", [])
        if not attached_volumes:
            return False

        # Verify each volume is actually queryable in the inventory
        for attached_volume in attached_volumes:
            volume_id = attached_volume.get("ID")
            if not volume_id:
                LOGGER.warning(f"VM '{vm_name}' has attached volume without ID - incomplete sync")
                return False

            try:
                self._request(url_path=f"{self.provider_url_path}/volumes/{volume_id}")
            except (ValueError, ConnectionError, TimeoutError) as e:
                LOGGER.warning(f"VM '{vm_name}' found but volume '{volume_id}' not yet queryable in inventory: {e}")
                return False

        return True

    def _check_openstack_networks_synced(self, vm: dict[str, Any], vm_name: str) -> bool:
        """Verify OpenStack VM's networks are synced to inventory.

        Args:
            vm (dict[str, Any]): VM data from Forklift inventory
            vm_name (str): VM name for logging

        Returns:
            bool: True if all networks synced, False if still waiting

        Raises:
            ValueError: If network query fails with validation error
            ConnectionError: If unable to connect to inventory API
            TimeoutError: If network query times out
        """
        vm_addresses = vm.get("addresses", {})
        if not vm_addresses:
            LOGGER.debug(f"VM '{vm_name}' found but has no addresses yet. Waiting for network sync.")
            return False

        # Get network names from VM addresses
        vm_network_names = set(vm_addresses.keys())

        # Get available networks from inventory
        try:
            inventory_network_names = {net["name"] for net in self.networks}
        except (ValueError, ConnectionError, TimeoutError) as e:
            LOGGER.warning(f"VM '{vm_name}' found but networks not yet queryable in inventory: {e}")
            return False

        # Check if all VM networks exist in inventory
        missing_networks = vm_network_names - inventory_network_names
        if missing_networks:
            LOGGER.debug(
                f"VM '{vm_name}' found but networks {missing_networks} not yet in inventory. "
                f"Available networks: {inventory_network_names}"
            )
            return False

        return True

    def wait_for_vm(self, name: str, timeout: int = 300, sleep: int = 10) -> dict[str, Any]:
        """Wait for a VM to appear in the Forklift inventory after cloning.

        For OpenStack VMs, also waits for attached volumes and networks to sync,
        as these are synced separately from VM metadata and are required for
        storage/network mapping.

        Args:
            name: VM name to wait for
            timeout: Maximum time to wait in seconds (default: 300)
            sleep: Time to sleep between checks in seconds (default: 10)

        Returns:
            VM dictionary from inventory

        Raises:
            TimeoutExpiredError: If VM doesn't appear within timeout or attached volumes/networks don't sync
        """
        LOGGER.info(f"Waiting for VM '{name}' to appear in Forklift inventory...")
        last_vm: dict[str, Any] = {}

        def _check_vm_ready() -> dict[str, Any] | None:
            """Check if VM exists and has all required data synced."""
            nonlocal last_vm
            try:
                vm = self.get_vm(name=name)
                last_vm = vm

                # For OpenStack, verify volumes and networks are synced
                if self.provider_type == Provider.ProviderType.OPENSTACK:
                    if not (
                        self._check_openstack_volumes_synced(vm, name)
                        and self._check_openstack_networks_synced(vm, name)
                    ):
                        return None

                return vm
            except ValueError:
                return None

        try:
            for sample in TimeoutSampler(
                wait_timeout=timeout,
                sleep=sleep,
                func=_check_vm_ready,
            ):
                if sample:
                    LOGGER.info(f"VM '{name}' found in inventory with all required data")
                    return sample
        except TimeoutExpiredError:
            if last_vm:
                raise TimeoutExpiredError(
                    f"VM '{name}' found in Forklift inventory but attached volumes or networks did not sync after {timeout}s. "
                    f"Attached volumes: {last_vm.get('attachedVolumes', [])}, "
                    f"VM addresses: {last_vm.get('addresses', {})}"
                )
            raise TimeoutExpiredError(
                f"VM '{name}' did not appear in Forklift inventory after {timeout}s. Available VMs: {self.vms_names}"
            )

        # This should never be reached, but satisfies type checker
        raise TimeoutExpiredError(f"VM '{name}' wait completed unexpectedly without returning")

    @property
    def vms_names(self) -> list[str]:
        _vms: list[str] = []
        for _vm in self.vms:
            _vms.append(_vm["name"])

        return _vms

    @property
    def networks(self) -> list[dict[str, Any]]:
        return self._request(url_path=f"{self.provider_url_path}/networks")

    @property
    @abc.abstractmethod
    def storages(self) -> list[dict[str, Any]]:
        pass

    @abc.abstractmethod
    def vms_storages_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        pass

    @abc.abstractmethod
    def vms_networks_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        pass


class OvirtForkliftInventory(ForkliftInventory):
    def __init__(self, client: DynamicClient, provider_name: str, namespace: str) -> None:
        self.provider_type = Provider.ProviderType.RHV
        super().__init__(
            client=client, provider_name=provider_name, mtv_namespace=namespace, provider_type=self.provider_type
        )

    @property
    def storages(self) -> list[dict[str, Any]]:
        return self._request(url_path=f"{self.provider_url_path}/storagedomains")

    def vms_storages_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []
        _storages = self.storages

        if not _storages:
            raise ValueError(f"Storages not found for provider {self.provider_type}")

        for _vm_name in vms:
            _vm = self.get_vm(name=_vm_name)

            for _disk in _vm.get("diskAttachments", []):
                _disk_id = _disk["id"]
                _disk_id_info = self._request(f"{self.provider_url_path}/disks/{_disk_id}")
                _storage_id = _disk_id_info["storageDomain"]
                if _storage_name_match := [_stg["name"] for _stg in _storages if _storage_id == _stg["id"]]:
                    _mappings.append({"name": _storage_name_match[0]})

        if not _mappings:
            raise ValueError(f"Storages not found for VMs {vms} on provider {self.provider_type}")

        return _mappings

    def vms_networks_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []

        for _vm_name in vms:
            _vm = self.get_vm(name=_vm_name)

            nic_profiles = self._request(f"{self.provider_url_path}/nicprofiles")
            for _network in _vm.get("nics", []):
                _network_profile = _network["profile"]

                for _nic_profile in nic_profiles:
                    if _nic_profile["id"] in _network_profile:
                        _selfLink = _nic_profile["selfLink"].replace("providers/", "")
                        _network_id = self._request(url_path=_selfLink)["network"]
                        if _network_name_match := [_net["path"] for _net in self.networks if _network_id == _net["id"]]:
                            if [_map for _map in _mappings if _map.get("name") == _network_name_match[0]]:
                                continue

                            _mappings.append({"name": _network_name_match[0]})

        if not _mappings:
            raise ValueError(f"Networks not found for vms {vms} on provider {self.provider_type}")

        return _mappings


class OpenstackForliftinventory(ForkliftInventory):
    def __init__(self, client: DynamicClient, provider_name: str, namespace: str) -> None:
        self.provider_type = Provider.ProviderType.OPENSTACK
        super().__init__(
            client=client, provider_name=provider_name, mtv_namespace=namespace, provider_type=self.provider_type
        )

    @property
    def storages(self) -> list[dict[str, Any]]:
        # OpenStack uses volume types, not storage domains
        return self._request(url_path=f"{self.provider_url_path}/volumes")

    def vms_storages_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        """Get storage mappings for OpenStack VMs based on volume types."""
        _mappings: list[dict[str, str]] = []

        for _vm_name in vms:
            _vm = self.get_vm(name=_vm_name)

            # Get volumes attached to this VM
            for attached_volume in _vm.get("attachedVolumes", []):
                volume_id = attached_volume.get("ID")
                if not volume_id:
                    continue

                # Get volume details to find volume type
                volume_info = self._request(url_path=f"{self.provider_url_path}/volumes/{volume_id}")
                volume_type = volume_info.get("volumeType")

                if volume_type and not any(m.get("name") == volume_type for m in _mappings):
                    _mappings.append({"name": volume_type})

        if not _mappings:
            raise ValueError(f"No storage volumes found for VMs {vms} on provider {self.provider_type}")

        return _mappings

    def vms_networks_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []

        for _vm_name in vms:
            _vm = self.get_vm(name=_vm_name)

            for _name in _vm.get("addresses", {}).keys():
                if _network_id_match := [_net["id"] for _net in self.networks if _name == _net["name"]]:
                    if [_map for _map in _mappings if _map.get("id") == _network_id_match[0]]:
                        continue

                    _mappings.append({"id": _network_id_match[0], "name": _name})

        if not _mappings:
            raise ValueError(f"Networks not found for vms {vms} on provider {self.provider_type}")

        return _mappings


class VsphereForkliftInventory(ForkliftInventory):
    def __init__(self, client: DynamicClient, provider_name: str, namespace: str) -> None:
        self.provider_type = Provider.ProviderType.VSPHERE
        super().__init__(
            client=client, provider_name=provider_name, mtv_namespace=namespace, provider_type=self.provider_type
        )

    @property
    def hosts(self) -> list[dict[str, Any]]:
        """Return all ESXi hosts from the Forklift vSphere inventory.

        Returns:
            list[dict[str, Any]]: Host objects from the provider's /hosts endpoint.
        """
        return self._request(url_path=f"{self.provider_url_path}/hosts")

    @property
    def storages(self) -> list[dict[str, Any]]:
        return self._request(url_path=f"{self.provider_url_path}/datastores")

    def wait_for_hosts(self, timeout: int = 300, sleep: int = 10) -> list[dict[str, Any]]:
        """Wait for hosts to appear in the Forklift inventory.

        Fresh vSphere providers report Ready while GET /hosts returns empty.
        This causes VM validation to fail with 'host id not found' errors.

        Args:
            timeout: Maximum time to wait in seconds (default: 300)
            sleep: Time to sleep between checks in seconds (default: 10)

        Returns:
            List of host dictionaries from inventory

        Raises:
            TimeoutExpiredError: If hosts don't appear within timeout
        """
        LOGGER.info(f"Waiting for hosts to appear in Forklift inventory for provider '{self.provider_name}'...")

        try:
            for sample in TimeoutSampler(
                wait_timeout=timeout,
                sleep=sleep,
                func=lambda: self.hosts,
            ):
                if sample:
                    LOGGER.info(f"Found {len(sample)} hosts in inventory for provider '{self.provider_name}'")
                    return sample
        except TimeoutExpiredError:
            pass

        raise TimeoutExpiredError(
            f"No hosts appeared in Forklift inventory for provider '{self.provider_name}' after {timeout}s"
        )

    def wait_for_datastores(
        self, datastore_ids: list[str], timeout: int = 300, sleep: int = 10
    ) -> list[dict[str, Any]]:
        """Wait for datastores to appear in the Forklift inventory.

        Fresh vSphere providers may report Ready while GET /datastores does not yet
        include all datastores referenced by cross-datastore disk configurations.

        Args:
            datastore_ids: MoIDs of datastores to wait for
            timeout: Maximum time to wait in seconds (default: 300)
            sleep: Time to sleep between checks in seconds (default: 10)

        Returns:
            List of storage dictionaries for the requested datastores

        Raises:
            TimeoutExpiredError: If any requested datastores don't appear within timeout
        """
        if not datastore_ids:
            return []

        requested_ids = set(datastore_ids)
        LOGGER.info(
            f"Waiting for datastores {sorted(requested_ids)} to appear in Forklift inventory "
            f"for provider '{self.provider_name}'..."
        )

        def _check_datastores() -> list[dict[str, Any]] | None:
            """Poll for all requested datastore IDs to appear in inventory.

            Returns:
                list[dict[str, Any]]: Matching storage entries when all requested IDs are present.
                None: If any requested IDs are still missing.
            """
            storages = self.storages
            found_ids = _extract_storage_ids(storages)
            if requested_ids - found_ids:
                return None
            return [storage for storage in storages if storage.get("id") in requested_ids]

        try:
            for sample in TimeoutSampler(
                wait_timeout=timeout,
                sleep=sleep,
                func=_check_datastores,
            ):
                if sample:
                    for storage in sample:
                        LOGGER.info(
                            f"Found datastore '{storage.get('id')}' in inventory for provider '{self.provider_name}'"
                        )
                    return sample
        except TimeoutExpiredError:
            pass

        found_ids = _extract_storage_ids(self.storages)
        missing_ids = sorted(requested_ids - found_ids)
        raise TimeoutExpiredError(
            f"Datastores {missing_ids} did not appear in Forklift inventory for provider "
            f"'{self.provider_name}' after {timeout}s"
        )

    def vms_storages_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []
        _storages = self.storages

        if not _storages:
            raise ValueError(f"Storages not found for provider {self.provider_type}")

        for _vm_name in vms:
            _vm = self.get_vm(name=_vm_name)
            for _disk in _vm.get("disks", []):
                if _storage_id := _disk.get("datastore", {}).get("id"):
                    if _storage_name_match := [_stg["name"] for _stg in _storages if _storage_id == _stg["id"]]:
                        _mappings.append({"name": _storage_name_match[0]})

        if not _mappings:
            raise ValueError(f"Storages not found for VMs {vms} on provider {self.provider_type}")

        return _mappings

    def vms_networks_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []

        for _vm_name in vms:
            _vm = self.get_vm(name=_vm_name)
            for _nic in _vm.get("nics", []):
                if _network_id := (_nic.get("network") or {}).get("id"):
                    if _network_name_match := [_net["name"] for _net in self.networks if _network_id == _net["id"]]:
                        if [_map for _map in _mappings if _map.get("name") == _network_name_match[0]]:
                            continue

                        _mappings.append({"name": _network_name_match[0]})

        if not _mappings:
            raise ValueError(f"Networks not found for vms {vms} on provider {self.provider_type}")

        return _mappings


class OvaForkliftInventory(ForkliftInventory):
    def __init__(self, client: DynamicClient, provider_name: str, namespace: str) -> None:
        self.provider_type = Provider.ProviderType.OVA
        super().__init__(
            client=client, provider_name=provider_name, mtv_namespace=namespace, provider_type=self.provider_type
        )

    @property
    def storages(self) -> list[dict[str, Any]]:
        return self._request(url_path=f"{self.provider_url_path}/storages")

    def vms_storages_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []
        _storages = self.storages

        if not _storages:
            raise ValueError(f"Storages not found for provider {self.provider_type}")

        for _storage in _storages:
            if [vm for vm in vms if vm in _storage["name"]]:
                _mappings.append({"id": _storage["id"]})

        if not _mappings:
            raise ValueError(f"Storages not found for VMs {vms} on provider {self.provider_type}")

        return _mappings

    def vms_networks_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []

        for _vm_name in vms:
            _vm = self.get_vm(name=_vm_name)

            for _network in _vm.get("networks", []):
                if _network_id := _network.get("ID"):
                    if _network_name_match := [_net["name"] for _net in self.networks if _network_id == _net["id"]]:
                        if [_map for _map in _mappings if _map.get("name") == _network_name_match[0]]:
                            continue

                        _mappings.append({"name": _network_name_match[0]})

        if not _mappings:
            raise ValueError(f"Networks not found for vms {vms} on provider {self.provider_type}")

        return _mappings


class OpenshiftForkliftInventory(ForkliftInventory):
    def __init__(self, client: DynamicClient, provider_name: str, namespace: str) -> None:
        self.provider_type = Provider.ProviderType.OPENSHIFT
        super().__init__(
            client=client, provider_name=provider_name, mtv_namespace=namespace, provider_type=self.provider_type
        )

    @property
    def storages(self) -> list[dict[str, Any]]:
        return self._request(url_path=f"{self.provider_url_path}/storageclasses")

    def vms_storages_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []

        for vm in vms:
            _vm = self.get_vm(name=vm)
            _namespace = _vm["object"]["metadata"]["namespace"]

            for _volume in _vm["object"]["spec"]["template"]["spec"]["volumes"]:
                if data_volume := _volume.get("dataVolume"):
                    _pvc = PersistentVolumeClaim(
                        name=data_volume["name"], namespace=_namespace, client=self.client, ensure_exists=True
                    )
                    _storage_class = _pvc.instance.spec.storageClassName

                    if [_map for _map in _mappings if _map.get("name") == _storage_class]:
                        continue

                    _mappings.append({"name": _storage_class})

        if not _mappings:
            raise ValueError(f"Storages not found for VMs {vms} on provider {self.provider_type}")

        return _mappings

    def vms_networks_mappings(self, vms: list[str]) -> list[dict[str, str]]:
        _mappings: list[dict[str, str]] = []

        for vm in vms:
            _vm = self.get_vm(name=vm)

            for _network in _vm["object"]["spec"]["template"]["spec"]["networks"]:
                _network_map = None

                if _multus := _network.get("multus"):
                    _network_map = {"name": _multus.get("networkName")}

                elif isinstance(_network.get("pod"), dict):
                    _network_map = {"type": "pod"}

                if _network_map:
                    if [_map for _map in _mappings if _map == _network_map]:
                        continue

                    _mappings.append(_network_map)

        if not _mappings:
            raise ValueError(f"Networks not found for vms {vms} on provider {self.provider_type}")

        return _mappings
