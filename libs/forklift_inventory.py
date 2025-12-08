import abc
from typing import Any

from kubernetes.dynamic.client import DynamicClient
from ocp_resources.persistent_volume_claim import PersistentVolumeClaim
from ocp_resources.provider import Provider
from ocp_resources.route import Route
from simple_logger.logger import get_logger
from timeout_sampler import TimeoutExpiredError, TimeoutSampler

LOGGER = get_logger(__name__)


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

    def wait_for_vm(self, name: str, timeout: int = 300, sleep: int = 10) -> dict[str, Any]:
        """Wait for a VM to appear in the Forklift inventory after cloning.

        For OpenStack VMs, also waits for attached volumes to sync, as volumes
        are synced separately from VM metadata and are required for storage mapping.

        Args:
            name: VM name to wait for
            timeout: Maximum time to wait in seconds (default: 300)
            sleep: Time to sleep between checks in seconds (default: 10)

        Returns:
            VM dictionary from inventory

        Raises:
            TimeoutExpiredError: If VM doesn't appear within timeout or attached volumes don't sync
        """
        LOGGER.info(f"Waiting for VM '{name}' to appear in Forklift inventory...")
        last_vm = None

        def _check_vm_ready() -> dict[str, Any] | None:
            """Check if VM exists and has all required data synced."""
            nonlocal last_vm
            try:
                vm = self.get_vm(name=name)
                last_vm = vm

                # For OpenStack VMs, verify attached volumes are synced
                if self.provider_type == Provider.ProviderType.OPENSTACK:
                    attached_volumes = vm.get("attachedVolumes", [])
                    if not attached_volumes:
                        LOGGER.debug(f"VM '{name}' found but attached volumes not yet synced, waiting...")
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
                    f"VM '{name}' found in Forklift inventory but attached volumes did not sync after {timeout}s. "
                    f"Attached volumes: {last_vm.get('attachedVolumes', [])}"
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
    def storages(self) -> list[dict[str, Any]]:
        return self._request(url_path=f"{self.provider_url_path}/datastores")

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
            for _network in _vm.get("networks", []):
                if _network_id := _network.get("id"):
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
