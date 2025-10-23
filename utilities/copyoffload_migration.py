"""
Copy-offload migration utilities for MTV tests.

This module provides copy-offload specific functionality for VM migration tests,
including storage secret creation and credential management.
"""
from __future__ import annotations

import os
from typing import Any

from kubernetes.dynamic import DynamicClient
from ocp_resources.secret import Secret
from simple_logger.logger import get_logger

from utilities.resources import create_and_store_resource

LOGGER = get_logger(__name__)


def get_copyoffload_credential(
    credential_name: str,
    copyoffload_config: dict[str, Any],
) -> str | None:
    """
    Get a copyoffload credential from environment variable or config file.

    Environment variables take precedence over config file values.

    Args:
        credential_name: Name of the credential (e.g., "storage_hostname", "ontap_svm")
        copyoffload_config: Copyoffload configuration dictionary

    Returns:
        str | None: Credential value from env var or config, or None if not found
    """
    env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
    return os.getenv(env_var_name) or copyoffload_config.get(credential_name)

def create_storage_secret_for_copyoffload(
    fixture_store: dict[str, Any],
    ocp_admin_client: DynamicClient,
    target_namespace: str,
    copyoffload_config: dict[str, Any],
) -> Secret:
    """
    Create a storage secret for copy-offload functionality.

    Args:
        fixture_store: Pytest fixture store for resource tracking
        ocp_admin_client: OpenShift admin client
        target_namespace: Target namespace for the secret
        copyoffload_config: Copy-offload configuration dictionary

    Returns:
        Secret: Created storage secret resource

    Raises:
        ValueError: If required copyoffload configuration is missing
    """
    # Get storage credentials from environment variables or provider config
    storage_hostname = get_copyoffload_credential("storage_hostname", copyoffload_config)
    storage_username = get_copyoffload_credential("storage_username", copyoffload_config)
    storage_password = get_copyoffload_credential("storage_password", copyoffload_config)

    if not all([storage_hostname, storage_username, storage_password]):
        raise ValueError(
            "Storage credentials are required. Set COPYOFFLOAD_STORAGE_HOSTNAME, COPYOFFLOAD_STORAGE_USERNAME, "
            "and COPYOFFLOAD_STORAGE_PASSWORD environment variables or include them in .providers.json"
        )

    # Validate storage vendor product
    storage_vendor = copyoffload_config.get("storage_vendor_product")
    if not storage_vendor:
        raise ValueError(
            "storage_vendor_product is required in copyoffload configuration. "
            "Valid values: 'ontap', 'vantara'"
        )

    # Base secret data
    secret_data = {
        "STORAGE_HOSTNAME": storage_hostname,
        "STORAGE_USERNAME": storage_username,
        "STORAGE_PASSWORD": storage_password,
    }

    # Add vendor-specific configuration
    if storage_vendor == "ontap":
        ontap_svm = get_copyoffload_credential("ontap_svm", copyoffload_config)
        if ontap_svm:
            secret_data["ONTAP_SVM"] = ontap_svm

    LOGGER.info(f"Creating storage secret for copy-offload with vendor: {storage_vendor}")

    storage_secret = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        namespace=target_namespace,
        string_data=secret_data,
    )

    return storage_secret
