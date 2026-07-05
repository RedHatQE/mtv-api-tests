from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.conversion import Conversion
from ocp_resources.secret import Secret

from utilities.deep_inspection import create_conversion_resource, create_di_connection_secret
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory


@pytest.fixture(scope="class")
def di_connection_secret(
    ocp_admin_client: "DynamicClient",
    fixture_store: dict[str, Any],
    source_provider: "BaseProvider",
    target_namespace: str,
) -> Secret:
    """Connection secret for standalone Deep Inspection.

    Copies source provider secret data and injects URL + fingerprint,
    replicating the forklift controller's connection secret building.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        source_provider (BaseProvider): Source provider with ocp_resource.
        target_namespace (str): Namespace for the secret.

    Returns:
        Secret: The created connection secret.
    """
    return create_di_connection_secret(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        source_provider=source_provider,
        target_namespace=target_namespace,
    )


@pytest.fixture(scope="class")
def di_vm_name(class_plan_config: dict[str, Any]) -> str:
    """First VM name from the plan config.

    Args:
        class_plan_config (dict[str, Any]): Raw plan config from parametrization.

    Returns:
        str: The VM name.
    """
    return class_plan_config["virtual_machines"][0]["name"]


@pytest.fixture(scope="class")
def di_conversion_resource(
    class_plan_config: dict[str, Any],
    di_connection_secret: Secret,
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
    source_provider_data: dict[str, Any],
    source_provider_inventory: "ForkliftInventory",
) -> Conversion:
    """Standalone DeepInspection Conversion CR for the first VM in the plan.

    Uses class_plan_config directly (not prepared_plan) because Deep Inspection
    only creates a snapshot, inspects, and removes the snapshot — no VM cloning
    is needed.

    Args:
        class_plan_config (dict[str, Any]): Raw plan config from parametrization.
        di_connection_secret (Secret): Connection secret for vSphere access.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace for the Conversion CR.
        source_provider_data (dict[str, Any]): Provider config from providers.json.
        source_provider_inventory (ForkliftInventory): Source provider inventory.

    Returns:
        Conversion: The created Conversion CR.

    Raises:
        ValueError: If vddk_init_image is missing from provider data.
    """
    plan_config = deepcopy(class_plan_config)
    populate_vm_ids(plan_config, source_provider_inventory)
    vm = plan_config["virtual_machines"][0]

    vddk_image = source_provider_data.get("vddk_init_image")
    if not vddk_image:
        raise ValueError("vddk_init_image missing from source_provider_data — required for DeepInspection")

    return create_conversion_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        connection_secret=di_connection_secret,
        vm_id=vm["id"],
        vm_name=vm["name"],
        vddk_image=vddk_image,
        target_namespace=target_namespace,
    )
