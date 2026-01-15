"""Tests for MTV 2.10.0 target scheduling features.

These tests verify targetNodeSelector, targetLabels, and targetAffinity features
introduced in MTV 2.10.0. Tests are skipped if MTV version < 2.10.0.
"""

import pytest
from pytest_testconfig import config as py_config

from utilities.mtv_migration import create_storagemap_and_networkmap, migrate_vms


@pytest.mark.parametrize(
    "plan,multus_network_name",
    [
        pytest.param(
            py_config["tests_params"]["test_target_scheduling_all_features"],
            py_config["tests_params"]["test_target_scheduling_all_features"],
        )
    ],
    indirect=True,
    ids=["target-all-features"],
)
@pytest.mark.tier0
@pytest.mark.min_mtv_version("2.10.0")
def test_target_scheduling_all_features(
    request,
    fixture_store,
    ocp_admin_client,
    target_namespace,
    destination_provider,
    plan,
    source_provider,
    source_provider_data,
    multus_network_name,
    source_provider_inventory,
    source_vms_namespace,
    labeled_worker_node,
    target_vm_labels,
):
    """Test all MTV 2.10.0 target scheduling features together.

    This test verifies:
    - targetNodeSelector: VM scheduled to labeled node
    - targetLabels: Custom labels applied to VM
    - targetAffinity: Pod affinity rules applied to VM

    Note:
        targetNodeSelector and targetLabels use 'auto' placeholders in config which are
        replaced with session_uuid by fixtures. targetAffinity is passed directly from plan.
    """
    storage_migration_map, network_migration_map = create_storagemap_and_networkmap(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        plan=plan,
    )

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
        labeled_worker_node=labeled_worker_node,
        target_vm_labels=target_vm_labels,
    )
