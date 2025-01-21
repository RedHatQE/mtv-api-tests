from typing import Any

import yaml
from ocp_resources.resource import NamespacedResource, Resource
from simple_logger.logger import get_logger

LOGGER = get_logger(__name__)


def create_and_store_resource(
    fixture_store: dict[str, Any], resource: type[Resource], **kwargs: Any
) -> Resource | NamespacedResource:
    _resource_name = kwargs.get("name", "")
    _resource_yaml = kwargs.get("yaml_file", "")

    if _resource_yaml:
        with open(_resource_yaml) as fd:
            yaml_data = yaml.safe_load(fd)

        _resource_name = yaml_data.get("metadata", {}).get("name", "")

    if not _resource_name.startswith("mtv-api-tests"):
        LOGGER.error(f"Resource name should start with mtv-api-tests: {_resource_name}")

    _resource = resource(**kwargs)
    _resource.deploy(wait=True)
    fixture_store["teardown"].setdefault(_resource.kind, []).append(_resource)
    return _resource
