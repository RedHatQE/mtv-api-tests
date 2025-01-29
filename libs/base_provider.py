from __future__ import annotations

import abc
from logging import Logger
from typing import Any

from ocp_resources.resource import Resource
from simple_logger.logger import get_logger


class BaseProvider(abc.ABC):
    # Unified Representation of a VM of All Provider Types
    VIRTUAL_MACHINE_TEMPLATE: dict[str, Any] = {
        "id": "",
        "name": "",
        "provider_type": "",  # "ovirt" / "vsphere" / "openstack"
        "provider_vm_api": None,
        "network_interfaces": [],
        "disks": [],
        "cpu": {},
        "memory_in_mb": 0,
        "snapshots_data": [],
        "power_state": "",
    }

    def __init__(
        self,
        ocp_resource: type[Resource],
        username: str | None = None,
        password: str | None = None,
        host: str | None = None,
        provider_data: dict[str, Any] | None = None,
        debug: bool = False,
        log: Logger | None = None,
    ) -> None:
        self.ocp_resource = ocp_resource

        if not self.ocp_resource:
            raise ValueError("ocp_resource is required, but not provided")

        self.username = username
        self.password = password
        self.host = host
        self.debug = debug
        self.log = log or get_logger(name=__name__)
        self.api: Any = None
        self.provider_data = provider_data

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return

    @abc.abstractmethod
    def connect(self) -> Any:
        pass

    @abc.abstractmethod
    def disconnect(self) -> Any:
        pass

    @property
    @abc.abstractmethod
    def test(self) -> bool:
        pass

    @abc.abstractmethod
    def vm_dict(self, **kwargs: Any) -> dict[str, Any]:
        """
        Create a dict for a single vm holding the Network Interface details, Disks and Storage, etc..
        """
        pass
