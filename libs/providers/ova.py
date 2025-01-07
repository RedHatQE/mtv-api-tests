from __future__ import annotations
from typing import Any

from ocp_resources.resource import Resource
from libs.base_provider import BaseProvider


class OVAProvider(BaseProvider):
    def __init__(self, ocp_resource: Resource, **kwargs: Any) -> None:
        super().__init__(ocp_resource=ocp_resource, **kwargs)

    def disconnect(self) -> None:
        return

    def connect(self) -> "OVAProvider":
        return self

    @property
    def test(self) -> bool:
        return True
