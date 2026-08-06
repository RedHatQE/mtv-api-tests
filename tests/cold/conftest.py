from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import pytest

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory, create_forklift_inventory
from utilities.utils import create_source_provider, get_value_from_py_config

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from ocp_resources.secret import Secret


@pytest.fixture(scope="class")  # Class-scoped: separate provider per test class with ca.crt in the secret
def ca_crt_source_provider(
    fixture_store: dict[str, Any],
    session_uuid: str,
    source_provider_data: dict[str, Any],
    target_namespace: str,
    ocp_admin_client: DynamicClient,
    tmp_path_factory: pytest.TempPathFactory,
    destination_ocp_secret: Secret,  # pragma: allowlist secret
) -> Generator[BaseProvider, None, None]:
    """Source provider configured with ca.crt secret field instead of cacert.

    Uses the standard Kubernetes ca.crt convention (MTV-4561) to verify
    Forklift accepts the new field name for CA certificates.

    Args:
        fixture_store (dict[str, Any]): Session fixture store for resource tracking.
        session_uuid (str): Unique session identifier.
        source_provider_data (dict[str, Any]): Provider configuration from providers JSON.
        target_namespace (str): Target namespace for provider resources.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        tmp_path_factory (pytest.TempPathFactory): Temp directory factory for cert files.
        destination_ocp_secret (Secret): Destination OCP cluster secret.

    Yields:
        BaseProvider: Source provider instance using ca.crt in the secret.
    """
    with create_source_provider(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider_data=source_provider_data,
        namespace=target_namespace,
        admin_client=ocp_admin_client,
        tmp_dir=tmp_path_factory,
        ocp_admin_client=ocp_admin_client,
        destination_ocp_secret=destination_ocp_secret,
        insecure=get_value_from_py_config(value="source_provider_insecure_skip_verify"),
        ca_cert_key="ca.crt",
    ) as _source_provider:
        yield _source_provider

    _source_provider.disconnect()


@pytest.fixture(scope="class")  # Class-scoped: must match ca_crt_source_provider scope
def ca_crt_source_provider_inventory(
    ocp_admin_client: DynamicClient,
    mtv_namespace: str,
    ca_crt_source_provider: BaseProvider,
) -> ForkliftInventory:
    """ForkliftInventory instance for the ca.crt provider.

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): MTV operator namespace.
        ca_crt_source_provider (BaseProvider): Source provider using ca.crt secret field.

    Returns:
        ForkliftInventory: Inventory instance for the ca.crt provider.
    """
    return create_forklift_inventory(
        client=ocp_admin_client,
        mtv_namespace=mtv_namespace,
        provider=ca_crt_source_provider,
    )


@pytest.fixture(scope="class")
def insecure_source_provider(
    fixture_store: dict[str, Any],
    session_uuid: str,
    source_provider_data: dict[str, Any],
    target_namespace: str,
    ocp_admin_client: DynamicClient,
    tmp_path_factory: pytest.TempPathFactory,
    destination_ocp_secret: Secret,  # pragma: allowlist secret
) -> Generator[BaseProvider, None, None]:
    """Source provider created with insecureSkipVerify=true (MTV-664).

    Args:
        fixture_store (dict[str, Any]): Session fixture store for resource tracking.
        session_uuid (str): Unique session identifier.
        source_provider_data (dict[str, Any]): Provider configuration from providers JSON.
        target_namespace (str): Target namespace for provider resources.
        ocp_admin_client (DynamicClient): OpenShift admin client.
        tmp_path_factory (pytest.TempPathFactory): Temp directory factory for cert files.
        destination_ocp_secret (Secret): Destination OCP cluster secret.

    Yields:
        BaseProvider: Source provider instance with insecureSkipVerify=true.
    """
    with create_source_provider(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider_data=source_provider_data,
        namespace=target_namespace,
        admin_client=ocp_admin_client,
        tmp_dir=tmp_path_factory,
        ocp_admin_client=ocp_admin_client,
        destination_ocp_secret=destination_ocp_secret,
        insecure=True,
    ) as _source_provider:
        yield _source_provider

    _source_provider.disconnect()


@pytest.fixture(scope="class")
def insecure_source_provider_inventory(
    ocp_admin_client: DynamicClient,
    mtv_namespace: str,
    insecure_source_provider: BaseProvider,
) -> ForkliftInventory:
    """ForkliftInventory instance for the insecureSkipVerify provider (MTV-664).

    Args:
        ocp_admin_client (DynamicClient): OpenShift admin client.
        mtv_namespace (str): MTV operator namespace.
        insecure_source_provider (BaseProvider): Source provider with insecureSkipVerify=true.

    Returns:
        ForkliftInventory: Inventory instance for the insecure provider.
    """
    return create_forklift_inventory(
        client=ocp_admin_client,
        mtv_namespace=mtv_namespace,
        provider=insecure_source_provider,
    )
