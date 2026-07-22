from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.conversion import Conversion
from ocp_resources.secret import Secret

from libs.providers.vmware import VMWareProvider
from utilities.deep_inspection import (
    create_conversion_resource,
    create_di_connection_secret,
)
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
def vmware_source_provider(source_provider: "BaseProvider") -> VMWareProvider:
    """Source provider validated as VMWare type.

    Snapshot operations (create, list, cleanup) require vSphere-specific
    APIs only available on VMWareProvider.

    Args:
        source_provider (BaseProvider): Source provider instance.

    Returns:
        VMWareProvider: The source provider, type-validated.

    Raises:
        TypeError: If source provider is not a VMWareProvider.
    """
    if not isinstance(source_provider, VMWareProvider):
        raise TypeError(f"Snapshot operations require VMWareProvider, got {type(source_provider).__name__}")
    return source_provider


@pytest.fixture(scope="class")
def di_resolved_vm(
    class_plan_config: dict[str, Any],
    source_provider_inventory: "ForkliftInventory",
) -> dict[str, Any]:
    """First VM from the plan config with populated inventory ID.

    Args:
        class_plan_config (dict[str, Any]): Raw plan config from parametrization.
        source_provider_inventory (ForkliftInventory): Source provider inventory.

    Returns:
        dict[str, Any]: VM config dict with populated 'name' and 'id' keys.
    """
    populate_vm_ids(class_plan_config, source_provider_inventory)
    return class_plan_config["virtual_machines"][0]


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
def di_vddk_image(source_provider_data: dict[str, Any]) -> str:
    """VDDK init container image from provider config.

    Args:
        source_provider_data (dict[str, Any]): Provider config from providers.json.

    Returns:
        str: The VDDK init container image.

    Raises:
        ValueError: If vddk_init_image is missing from provider data.
    """
    vddk_image = source_provider_data.get("vddk_init_image")
    if not vddk_image:
        raise ValueError("vddk_init_image missing from source_provider_data — required for DeepInspection")
    return vddk_image


@pytest.fixture(scope="class")
def di_conversion_resource(
    di_resolved_vm: dict[str, Any],
    di_connection_secret: Secret,
    di_vddk_image: str,
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
) -> Conversion:
    """Standalone DeepInspection Conversion CR for the first VM.

    Bypasses prepared_plan because Deep Inspection only creates a snapshot,
    inspects, and removes it — no VM cloning is needed.

    Args:
        di_resolved_vm (dict[str, Any]): VM config dict with 'name' and 'id'.
        di_connection_secret (Secret): Connection secret for vSphere access.
        di_vddk_image (str): VDDK init container image.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace for the Conversion CR.

    Returns:
        Conversion: The created Conversion CR.
    """
    return create_conversion_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        connection_secret=di_connection_secret,
        vm_id=di_resolved_vm["id"],
        vm_name=di_resolved_vm["name"],
        vddk_image=di_vddk_image,
        target_namespace=target_namespace,
    )


@pytest.fixture(scope="class")
def di_invalid_conversion_resource(
    di_resolved_vm: dict[str, Any],
    di_connection_secret: Secret,
    fixture_store: dict[str, Any],
    ocp_admin_client: "DynamicClient",
    target_namespace: str,
) -> Conversion:
    """Conversion CR with missing vddkImage to trigger validation error.

    Creates a DeepInspection Conversion CR without the required vddkImage
    field. The controller's validateVDDKImage() sets a Critical condition
    and blocks pipeline execution.

    Args:
        di_resolved_vm (dict[str, Any]): Resolved VM dict with 'name' and 'id'.
        di_connection_secret (Secret): Connection secret for vSphere access.
        fixture_store (dict[str, Any]): Resource tracking dictionary.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        target_namespace (str): Namespace for the Conversion CR.

    Returns:
        Conversion: The created Conversion CR (will have Critical conditions).
    """
    return create_conversion_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        connection_secret=di_connection_secret,
        vm_id=di_resolved_vm["id"],
        vm_name=di_resolved_vm["name"],
        target_namespace=target_namespace,
    )
