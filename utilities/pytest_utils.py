import json
from pathlib import Path
from typing import Any

from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def session_teardown(session_store: dict[str, Any]) -> None:
    for _resource_kind, _resource_list in session_store["teardown"].items():
        for _resource in _resource_list:
            try:
                _resource.clean_up(wait=True)
            except Exception as ex:
                LOGGER.error(f"Failed to clean up {_resource.name} due to: {ex}")


def collect_created_resources(session_store: dict[str, Any], log_collector_path: Path) -> None:
    _created_reousrces: dict[str, list[dict[str, str]]] = {}

    for _resource_kind, _resource_list in session_store["teardown"].items():
        _created_reousrces.setdefault(_resource_kind, [])
        for _resource in _resource_list:
            try:
                _created_reousrces[_resource_kind].append({
                    "module": _resource.__module__,
                    "name": _resource.name,
                    "namespace": _resource.namespace,
                })

            except Exception as ex:
                LOGGER.error(f"Failed to collect resource {_resource.name} data due to: {ex}")

    try:
        with open(log_collector_path / "resources.json", "w") as fd:
            json.dump(_created_reousrces, fd)
    except Exception as ex:
        LOGGER.error(f"Failed to store resources.json due to: {ex}")
