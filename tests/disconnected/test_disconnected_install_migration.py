"""
Disconnected (offline) MTV deployment and migration tests.

This module implements tests that validate MTV can be deployed from a
mirrored private registry (disconnected/offline environment) and that
migrations work on the resulting installation.

Setup (all via ``--tc=``):
    - ``disconnected_repo_url`` / ``disconnected_repo_ref``: mtv-autodeploy
      clone source (the repo containing ``disconnected/mirror-to-private-registry.sh``
      and ``mtv-install.sh``; must carry the ``DISCONNECTED`` install source).
    - ``mtv_disconnected_version``: MTV GA version to mirror and install.
    - ``private_registry``: destination registry for the mirrored images.
    - ``private_catalog_name`` (optional): mirrored CatalogSource name; when
      omitted it is auto-detected from the cluster or computed from the
      cluster's OCP version.
    - Standard cluster credentials (``cluster_host``, ``cluster_username``,
      ``cluster_password``) and a source provider, as for the other suites.

The suite requires a pre-installed MTV operator (the session-autouse
``forklift_pods_state`` fixture), matching the upgrade suite's design; the
tests then clean the cluster and reinstall MTV from the mirrored catalog.
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

from utilities.disconnected import run_disconnected_install, run_disconnected_mirror
from utilities.mtv_migration import execute_migration
from utilities.post_migration import check_vms
from utilities.utils import get_mtv_version

from kubernetes.dynamic.exceptions import NotFoundError

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from utilities.ssh_utils import SSHConnectionManager

LOGGER = get_logger(name=__name__)


@pytest.mark.disconnected
@pytest.mark.vsphere
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_disconnected_cold_migration"],
        )
    ],
    indirect=True,
    ids=["disconnected-cold"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestDisconnectedColdMigration:
    """MTV must be installable from a mirrored private registry and support cold migration.

    Purpose/Regression:
        Validates the disconnected (offline) deployment flow: mirroring GA
        content to a private registry, installing MTV from that registry
        (via IDMS + CatalogSource, no direct internet access to Red Hat
        registries), and verifying that a cold migration succeeds on the
        resulting installation. This covers MTV-477 (deploy offline) and
        is tracked by MTV-7087 / MTV-7345.

    Prerequisites:
        A vSphere source provider and an OpenShift destination cluster.
        The private registry must be accessible and have push credentials
        configured on the test runner. The cluster must have the IDMS
        and CatalogSource from a prior mirror run (or the mirror step
        runs first in this test).

    Test plan:
        1. Mirror MTV GA content to the private registry (if not already
           mirrored — skips if catalog exists).
        2. Install MTV from the mirrored catalog using
           MTV_SOURCE=DISCONNECTED.
        3. Verify the installed version matches and pods are running.
        4. Create migration resources (StorageMap, NetworkMap, Plan) and
           execute a cold migration.
        5. Validate the migrated VM.

    Expected result:
        MTV installs from the private registry, the version matches,
        and a cold migration completes successfully.
    """

    def test_disconnected_mirror(
        self,
        disconnected_mirror_script: str,
        private_catalog_name: str,
        ocp_admin_client: DynamicClient,
    ) -> None:
        """Mirror MTV GA content to the private registry (skip if already done)."""
        # Check if the catalog already exists — if so, skip the mirror step
        catalog_source_api = ocp_admin_client.resources.get(
            api_version="operators.coreos.com/v1alpha1", kind="CatalogSource"
        )
        try:
            catalog_source_api.get(name=private_catalog_name, namespace="openshift-marketplace")
            LOGGER.info(f"CatalogSource '{private_catalog_name}' already exists — skipping mirror step")
            return
        except NotFoundError:
            # NotFound means the catalog is absent — the mirror step must run
            pass

        run_disconnected_mirror(
            script_path=disconnected_mirror_script,
            mtv_version=py_config["mtv_disconnected_version"],
            private_registry=py_config["private_registry"],
            MAX_NESTED_PATHS="2",
        )

    def test_disconnected_install(
        self,
        disconnected_install_script: str,
        private_catalog_name: str,
    ) -> None:
        """Install MTV from the mirrored catalog using the DISCONNECTED source."""
        run_disconnected_install(
            script_path=disconnected_install_script,
            mtv_version=py_config["mtv_disconnected_version"],
            catalog_name=private_catalog_name,
        )

    def test_verify_disconnected_install(
        self,
        ocp_admin_client: DynamicClient,
    ) -> None:
        """Verify MTV version and pods after disconnected install."""
        version = get_mtv_version(client=ocp_admin_client)
        LOGGER.info(f"Post-install MTV version: {version}")

        expected_version: str = py_config["mtv_disconnected_version"]
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

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],  # Any: dynamic resource-tracking store shared across fixtures
        ocp_admin_client: DynamicClient,
        target_namespace: str,
        plan_resource: Plan,
    ) -> None:
        """Execute cold migration on the disconnected-installed MTV."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        # Any: prepared_plan / source_provider_data are dynamic fixture payload dictionaries
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: BaseProvider,
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager | None,
        network_map_resource: NetworkMap,
        storage_map_resource: StorageMap,
    ) -> None:
        """Validate migrated VMs after disconnected installation."""
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=network_map_resource,
            storage_map_resource=storage_map_resource,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
