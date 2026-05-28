"""
Upgrade migration tests for MTV.

This module implements tests that validate migration behavior across MTV
operator upgrades. The test flow creates migration resources (StorageMap,
NetworkMap, Plan) on the pre-upgrade version, upgrades the MTV operator,
verifies post-upgrade state, and then executes the migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from packaging.version import InvalidVersion, Version
from pytest_testconfig import config as py_config
from simple_logger.logger import get_logger

from utilities.mtv_migration import execute_migration
from utilities.post_migration import check_vms
from utilities.upgrade import run_mtv_upgrade
from utilities.utils import get_mtv_version

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from utilities.ssh_utils import SSHConnectionManager

LOGGER = get_logger(name=__name__)


@pytest.mark.upgrade
@pytest.mark.vsphere
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_upgrade_cold_migration"],
        )
    ],
    indirect=True,
    ids=["upgrade-cold"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestUpgradeColdMigration:
    """Cold migration with MTV operator upgrade between plan creation and execution."""

    def test_upgrade_mtv(self, upgrade_script_path: str, pre_upgrade_plan_resource: Plan) -> None:
        """Upgrade the MTV operator to the target version."""
        run_mtv_upgrade(
            script_path=upgrade_script_path,
            mtv_version=py_config["mtv_upgrade_to_version"],
            mtv_source=py_config["mtv_upgrade_to_source"],
            image_index=py_config["mtv_upgrade_image_index"],
        )

    def test_verify_post_upgrade(
        self,
        ocp_admin_client: DynamicClient,
        pre_upgrade_plan_resource: Plan,
    ) -> None:
        """Verify MTV version, pod readiness, and Plan CR state after upgrade."""
        version = get_mtv_version(client=ocp_admin_client)
        LOGGER.info(f"Post-upgrade MTV version: {version}")

        expected_version: str = py_config["mtv_upgrade_to_version"]
        try:
            actual = Version(version)
            expected = Version(expected_version)
        except InvalidVersion as exc:
            raise AssertionError(
                f"Could not parse MTV version strings: actual='{version}', expected='{expected_version}'"
            ) from exc
        assert (actual.major, actual.minor) == (expected.major, expected.minor), (
            f"Expected MTV major.minor version '{expected.major}.{expected.minor}', got '{version}'"
        )

        pre_upgrade_plan_resource.wait_for_condition(
            condition=Plan.Condition.READY,
            status=Plan.Condition.Status.TRUE,
            timeout=300,
        )
        LOGGER.info(f"Plan '{pre_upgrade_plan_resource.name}' is in Ready state after upgrade")

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        pre_upgrade_plan_resource: Plan,
    ) -> None:
        """Execute migration on the upgraded MTV operator."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=pre_upgrade_plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
        pre_upgrade_network_map: NetworkMap,
        pre_upgrade_storage_map: StorageMap,
    ) -> None:
        """Validate migrated VMs after upgrade."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=pre_upgrade_network_map,
            storage_map_resource=pre_upgrade_storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
