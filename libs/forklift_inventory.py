from typing import Any

from kubernetes.dynamic.client import DynamicClient
from ocp_resources.route import Route


class ForkliftInventory:
    def __init__(self, client: DynamicClient, provider_name: str, namespace: str) -> None:
        self.route = Route(client=client, name="forklift-inventory", namespace=namespace)
        self.provider_name = provider_name
        self.provider_type = self._get_type_by_name()
        self.provider_id = self._provider_id
        self.provider_url_path = f"{self.provider_type}/{self.provider_id}"
        self.vms_path = f"{self.provider_url_path}/vms"

    def _request(self, url_path: str = "") -> Any:
        return self.route.api_request(
            method="GET",
            url=f"https://{self.route.host}",
            action=f"providers{f'/{url_path}' if url_path else ''}",
        )

    def _get_type_by_name(self) -> str:
        for _provider_type, _provider_data in self._request().items():
            for _provider in _provider_data:
                if _provider["name"] == self.provider_name:
                    return _provider_type

        raise ValueError(f"Provider {self.provider_name} not found")

    @property
    def _provider_id(self) -> str:
        for _provider in self._request(url_path=self.provider_type):
            if _provider["name"] == self.provider_name:
                return _provider["id"]

        raise ValueError(f"Provider {self.provider_name} not found")

    def get_data(self) -> dict[str, Any]:
        return self._request(url_path=self.provider_url_path)

    @property
    def vms(self) -> list[dict[str, Any]]:
        return self._request(url_path=self.vms_path)

    def get_vm(self, name: str) -> dict[str, Any]:
        for _vm in self.vms:
            if _vm["name"] == name:
                return self._request(url_path=f"{self.vms_path}/{_vm['id']}")

        raise ValueError(f"VM {name} not found")

    @property
    def vms_names(self) -> list[str]:
        _vms: list[str] = []
        for _vm in self.vms:
            _vms.append(_vm["name"])

        return _vms
