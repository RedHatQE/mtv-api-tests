from __future__ import annotations

import abc
from logging import Logger
from typing import Any

from ocp_resources.provider import Provider
from simple_logger.logger import get_logger

from utilities.naming import generate_name_with_uuid


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
        fixture_store: dict[str, Any] | None = None,
        ocp_resource: Provider | None = None,
        username: str | None = None,
        password: str | None = None,
        host: str | None = None,
        debug: bool = False,
        log: Logger | None = None,
    ) -> None:
        self.ocp_resource = ocp_resource

        self.type = ""
        self.username = username
        self.password = password
        self.host = host
        self.debug = debug
        self.log = log or get_logger(name=__name__)
        self.api: Any = None
        self.fixture_store = fixture_store

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

    def _generate_clone_vm_name(self, session_uuid: str, base_name: str) -> str:
        """
        Generate a unique clone VM name with UUID and truncate if needed.

        Args:
            session_uuid: The session UUID to prefix the name
            base_name: The base name for the cloned VM

        Returns:
            A unique VM name, truncated to 63 chars if needed (keeping last 63 chars to preserve UUID)
        """
        clone_vm_name = generate_name_with_uuid(f"{session_uuid}-{base_name}")
        if len(clone_vm_name) > 63:
            self.log.warning(f"VM name '{clone_vm_name}' is too long ({len(clone_vm_name)} > 63). Truncating.")
            clone_vm_name = clone_vm_name[-63:]
        return clone_vm_name

    @abc.abstractmethod
    def clone_vm(self, source_vm_name: str, clone_vm_name: str, session_uuid: str, **kwargs: Any) -> Any:
        pass

    @abc.abstractmethod
    def delete_vm(self, vm_name: str) -> Any:
        pass
