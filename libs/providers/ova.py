from typing import Any
from libs.base_provider import BaseProvider


class OVAProvider(BaseProvider):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def disconnect(self) -> None:
        return

    def connect(self) -> "OVAProvider":
        return self

    @property
    def test(self) -> bool:
        return True
