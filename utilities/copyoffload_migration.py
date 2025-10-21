"""
Copy-offload migration utilities for MTV tests.

This module provides copy-offload specific functionality for VM migration tests,
including storage secret creation, copy-offload storage maps, and enhanced
migration workflows that use the vsphere-xcopy-volume-populator.
"""
from __future__ import annotations

import os
from typing import Any

from kubernetes.dynamic import DynamicClient
from ocp_resources.secret import Secret
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from utilities.mtv_migration import get_storage_migration_map, migrate_vms
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


def migrate_vms_with_copyoffload(
    ocp_admin_client: DynamicClient,
    request,
    fixture_store: dict[str, Any],
    source_provider: BaseProvider,
    destination_provider: BaseProvider,
    plan: dict[str, Any],
    network_migration_map,
    source_provider_data: dict[str, Any],
    target_namespace: str,
    source_vms_namespace: str,
    source_provider_inventory: ForkliftInventory,
) -> None:
    """Migrate VMs using copy-offload functionality."""
    if "copyoffload" not in source_provider_data:
        raise ValueError("copyoffload configuration not found in source provider data")

    copyoffload_config = source_provider_data["copyoffload"]

    LOGGER.info("Starting copy-offload migration")
    LOGGER.info(f"VMs to migrate: {[vm['name'] for vm in plan['virtual_machines']]}")

    # Create storage secret
    storage_secret = create_storage_secret_for_copyoffload(
        ocp_admin_client=ocp_admin_client,
        fixture_store=fixture_store,
        target_namespace=target_namespace,
        copyoffload_config=copyoffload_config,
    )

    # Get configuration values
    storage_vendor_product = copyoffload_config.get("storage_vendor_product")
    datastore_id = copyoffload_config.get("datastore_id")
    if not datastore_id:
        raise ValueError("datastore_id not found in copyoffload configuration")

    storage_class = py_config["storage_class"]
    LOGGER.info(f"Storage vendor: {storage_vendor_product}")
    LOGGER.info(f"Storage class: {storage_class}")

    # Build offload plugin configuration
    offload_plugin_config = {
        "vsphereXcopyConfig": {
            "secretRef": storage_secret.name,
            "storageVendorProduct": storage_vendor_product,
        }
    }

    # Use consolidated storage map creation function with copy-offload parameters
    vms = [vm["name"] for vm in plan["virtual_machines"]]

    storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        target_namespace=target_namespace,
        source_provider=source_provider,
        destination_provider=destination_provider,
        ocp_admin_client=ocp_admin_client,
        source_provider_inventory=source_provider_inventory,
        vms=vms,
        storage_class=storage_class,
        # Copy-offload specific parameters trigger copy-offload mode
        datastore_id=datastore_id,
        offload_plugin_config=offload_plugin_config,
        access_mode="ReadWriteOnce",
        volume_mode="Block",
    )

    # Execute migration
    migrate_vms(
        ocp_admin_client=ocp_admin_client,
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
        source_provider_inventory=source_provider_inventory,
    )

