from typing import Any

import yaml
from ocp_resources.resource import NamespacedResource, Resource
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def create_and_store_resource(
    fixture_store: dict[str, Any], resource: type[Resource], session_uuid: str, **kwargs: Any
) -> Resource | NamespacedResource:
    _resource_name = kwargs.get("name", "")
    _resource_yaml = kwargs.get("yaml_file", "")

    if _resource_yaml:
        with open(_resource_yaml) as fd:
            yaml_data = yaml.safe_load(fd)

        _resource_name = yaml_data.get("metadata", {}).get("name", "")

    if _resource_name and not _resource_name.startswith(session_uuid):
        LOGGER.error(f"Resource name should start with {session_uuid}: {_resource_name}")

    _resource = resource(**kwargs)
    _resource.deploy(wait=True)

    LOGGER.info(f"Storing {_resource.kind} {_resource_name} in fixture store")
    _resource_dict = {"name": _resource.name, "namespace": _resource.namespace, "module": _resource.__module__}
    fixture_store["teardown"].setdefault(_resource.kind, []).append(_resource_dict)

    return _resource
