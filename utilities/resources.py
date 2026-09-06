from typing import TYPE_CHECKING, Any

import yaml
from kubernetes.dynamic.exceptions import ConflictError
from ocp_resources.migration import Migration
from ocp_resources.namespace import Namespace
from ocp_resources.plan import Plan
from ocp_resources.resource import Resource
from simple_logger.logger import get_logger

from utilities.naming import generate_name_with_uuid

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

LOGGER = get_logger(__name__)


def create_and_store_resource(
    client: "DynamicClient",
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


def unregister_teardown_resource(
    fixture_store: dict[str, Any],  # Any: pytest fixture_store has dynamic teardown structure
    kind: str,
    name: str,
    namespace: str | None = None,
) -> None:
    """Remove a resource entry from fixture_store teardown tracking.

    Use after intentionally deleting a resource mid-test so session teardown
    does not operate on a missing object. Safe to call even if the entry
    does not exist — logs a warning instead of raising.

    When ``namespace`` is provided, only the entry matching both name and
    namespace is removed. When omitted, only the first name match is removed.

    Args:
        fixture_store (dict[str, Any]): Fixture store for resource tracking.
        kind (str): Resource kind key in fixture_store["teardown"].
        name (str): Resource name to unregister.
        namespace (str | None): Namespace stored on the teardown entry. When
            omitted, only the first name match is removed.

    Returns:
        None
    """
    teardown = fixture_store.get("teardown")
    if teardown is None:
        LOGGER.warning("fixture_store has no 'teardown' key — nothing to unregister")
        return

    resources = list(teardown.get(kind, []))
    match_indexes = [
        index
        for index, resource in enumerate(resources)
        if resource["name"] == name and (namespace is None or resource.get("namespace") == namespace)
    ]
    if not match_indexes:
        namespace_msg = f" in namespace '{namespace}'" if namespace is not None else ""
        LOGGER.warning(
            f"Resource '{name}' of kind '{kind}'{namespace_msg} not found in fixture_store teardown — already removed?"
        )
        return

    if namespace is None and len(match_indexes) > 1:
        LOGGER.warning(
            f"Multiple teardown entries for kind '{kind}' name '{name}'; unregistering only the first. "
            "Pass namespace= to select a specific entry."
        )

    del resources[match_indexes[0]]
    teardown[kind] = resources


def get_or_create_namespace(
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    namespace_name: str,
) -> str:
    """Get or create a namespace, ensuring it exists and is active.

    Checks if namespace exists. If not, creates it with standard labels.
    Only adds to fixture_store teardown if creating new namespace (not if reusing existing).

    Args:
        fixture_store (dict[str, Any]): Fixture store for resource tracking
        ocp_admin_client (DynamicClient): OpenShift client
        namespace_name (str): Name of the namespace

    Returns:
        str: The namespace name

    Raises:
        ValueError: If both yaml_file and kind_dict are specified in create_and_store_resource
        Exception: If namespace creation, deployment, or status check fails
    """
    ns = Namespace(name=namespace_name, client=ocp_admin_client)
    if ns.exists:
        LOGGER.info(f"Namespace {namespace_name} already exists, using it")
    else:
        LOGGER.info(f"Creating namespace {namespace_name}")
        ns = create_and_store_resource(
            fixture_store=fixture_store,
            resource=Namespace,
            client=ocp_admin_client,
            name=namespace_name,
            label={
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "mutatevirtualmachines.kubemacpool.io": "ignore",
            },
        )
    ns.wait_for_status(status=ns.Status.ACTIVE)
    return namespace_name
