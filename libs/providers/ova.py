from __future__ import annotations

import copy
from typing import Any, Self

from ocp_resources.provider import Provider
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider

LOGGER = get_logger(__name__)


class OVAProvider(BaseProvider):
    def __init__(self, ocp_resource: Provider | None = None, **kwargs: Any) -> None:
        super().__init__(ocp_resource=ocp_resource, **kwargs)
        self.type = Provider.ProviderType.OVA

    def disconnect(self) -> None:
        LOGGER.info("Disconnecting OVAProvider source provider")
        return

    def connect(self) -> Self:
        return self

    @property
    def test(self) -> bool:
        return True

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        result_vm_info = copy.deepcopy(self.VIRTUAL_MACHINE_TEMPLATE)
        result_vm_info["provider_type"] = self.type
        result_vm_info["power_state"] = "off"

        return result_vm_info

    def clone_vm(self, source_vm_name: str, clone_vm_name: str, session_uuid: str, **kwargs: Any) -> Any:
        return

    def delete_vm(self, vm_name: str) -> Any:
        return
