from typing import Any

from kubernetes.dynamic.client import DynamicClient
from ocp_resources.route import Route


class ForkliftInventory:
    def __init__(self, client: DynamicClient, provider_name: str, provider_type: str, namespace: str) -> None:
        self.route = Route(client=client, name="forklift-inventory", namespace=namespace)
        self.provider_name = provider_name
        self.provider_type = provider_type
        self.provider_id = self._provider_id

    def _request(self, url_path: str = "") -> Any:
        action = f"providers/{self.provider_type}"
        if url_path:
            action = f"{action}/{url_path}"

        return self.route.api_request(
            method="GET",
            url=f"https://{self.route.host}",
            action=action,
        )

    @property
    def _provider_id(self) -> str:
        for _provider in self._request():
            if _provider["name"] == self.provider_name:
                return _provider["id"]

        raise ValueError(f"Provider {self.provider_name} not found")

    def get_data(self) -> dict[str, Any]:
        return self._request(url_path=self.provider_id)

    @property
    def vms(self) -> list[dict[str, Any]]:
        return self._request(url_path=f"{self.provider_id}/vms")

    def get_vm(self, name: str) -> dict[str, Any]:
        for _vm in self.vms:
            if _vm["name"] == name:
                return self._request(url_path=f"{self.provider_id}/vms/{_vm['id']}")

        raise ValueError(f"VM {name} not found")

    def vms_names(self) -> list[str]:
        _vms: list[str] = []
        for _vm in self.vms:
            _vms.append(_vm["name"])

        return _vms
