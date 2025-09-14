from typing import Any

import yaml
from kubernetes.dynamic import DynamicClient
from kubernetes.dynamic.exceptions import ConflictError
from ocp_resources.migration import Migration
from ocp_resources.plan import Plan
from ocp_resources.resource import Resource
from simple_logger.logger import get_logger

from utilities.naming import generate_name_with_uuid

LOGGER = get_logger(__name__)


def create_and_store_resource(
    client: DynamicClient,
    fixture_store: dict[str, Any],
    resource: type[Resource],
    test_name: str | None = None,
    **kwargs: Any,
) -> Any:
    kwargs["client"] = client

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
        _resource_name = generate_name_with_uuid(name=fixture_store["base_resource_name"])

        if resource.kind in (Migration.kind, Plan.kind):
            _resource_name = f"{_resource_name}-{'warm' if kwargs.get('warm_migration') else 'cold'}"

    if len(_resource_name) > 63:
        LOGGER.warning(f"'{_resource_name=}' is too long ({len(_resource_name)} > 63). Truncating.")
        _resource_name = _resource_name[-63:]

    kwargs["name"] = _resource_name

    _resource = resource(**kwargs)

    try:
        _resource.deploy(wait=True)
    except ConflictError:
        LOGGER.warning(f"{_resource.kind} {_resource_name} already exists, reusing it.")
        _resource.wait()

    LOGGER.info(f"Storing {_resource.kind} {_resource.name} in fixture store")
    _resource_dict = {"name": _resource.name, "namespace": _resource.namespace, "module": _resource.__module__}

    if test_name:
        _resource_dict["test_name"] = test_name

    fixture_store["teardown"].setdefault(_resource.kind, []).append(_resource_dict)

    return _resource
