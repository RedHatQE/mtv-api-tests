from __future__ import annotations

from typing import Any

from ocp_resources.provider import Provider
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider

LOGGER = get_logger(__name__)


class OVAProvider(BaseProvider):
    def __init__(self, ocp_resource: Provider, **kwargs: Any) -> None:
        super().__init__(ocp_resource=ocp_resource, **kwargs)
        self.type = Provider.ProviderType.OVA

    def disconnect(self) -> None:
        LOGGER.info("Disconnecting OVAProvider source provider")
        return

    def connect(self) -> "OVAProvider":
        return self

    @property
    def test(self) -> bool:
        return True

    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        return {}
