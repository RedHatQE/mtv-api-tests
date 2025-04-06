from typing import Any

import yaml
from ocp_resources.plan import Plan
from ocp_resources.resource import Resource
from simple_logger.logger import get_logger

from exceptions.exceptions import ResourceNameNotStartedWithSessionUUIDError

LOGGER = get_logger(__name__)


def create_and_store_resource(
    fixture_store: dict[str, Any],
    resource: type[Resource],
    session_uuid: str,
    test_name: str | None = None,
    **kwargs: Any,
) -> Any:
    _resource_name = kwargs.get("name")
    _resource_dict = kwargs.get("kind_dict", {})
    _resource_yaml = kwargs.get("yaml_file")

    if _resource_yaml and _resource_dict:
        raise ValueError("Cannot specify both yaml_file and kind_dict")

    if not _resource_name:
        if _resource_yaml:
            with open(_resource_yaml) as fd:
                _resource_dict = yaml.safe_load(fd)

        _resource_name = _resource_dict.get("metadata", {}).get("name")

    if not _resource_name:
        raise ValueError("Resource name is required, but not provided. please provide name or yaml_file or kind_dict")

    if not _resource_name.startswith(session_uuid):
        raise ResourceNameNotStartedWithSessionUUIDError(
            f"Resource name should start with {session_uuid}: {_resource_name}"
        )

    _resource = resource(**kwargs)
    _resource.deploy(wait=True)

    LOGGER.info(f"Storing {_resource.kind} {_resource_name} in fixture store")
    _resource_dict = {"name": _resource.name, "namespace": _resource.namespace, "module": _resource.__module__}
    fixture_store["teardown"].setdefault(_resource.kind, []).append(_resource_dict)

    # Store plan name under test name key for collecting must gather by plan if test failed
    if test_name and _resource.kind == Plan.kind:
        LOGGER.info(
            f"Storing plan name {_resource.name} under test name {test_name} key for collecting must gather by plan"
        )
        fixture_store.setdefault(test_name, {}).setdefault("plans", []).append(_resource.name)

    return _resource
