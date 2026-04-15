import pytest
from typing import TYPE_CHECKING, Any
from ocp_resources.config_map import ConfigMap
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.provider import Provider
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.migration_utils import get_cutover_value
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms
from utilities.utils import load_source_providers, populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager

_SOURCE_PROVIDER_TYPE = load_source_providers().get(py_config.get("source_provider", ""), {}).get("type")


pytestmark = [
    pytest.mark.skipif(
        _SOURCE_PROVIDER_TYPE
        in (Provider.ProviderType.OPENSTACK, Provider.ProviderType.OPENSHIFT, Provider.ProviderType.OVA),
        reason=f"{_SOURCE_PROVIDER_TYPE} warm migration is not supported.",
    ),
]

# Only apply Jira marker for RHV - skip if issue unresolved, run normally if resolved
if _SOURCE_PROVIDER_TYPE == Provider.ProviderType.RHV:
    pytestmark.append(pytest.mark.jira("MTV-2846", run=False))


@pytest.mark.tier2
@pytest.mark.warm
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_warm_virt_customize_firstboot"],
        )
    ],
    indirect=True,
    ids=["virt-customize-firstboot"],
)
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestWarmVirtCustomizeFirstboot:
    """Warm migration test with virt-customize firstboot scripts.

    Tests the MTV virt-customize feature using ConfigMap with firstboot and run scripts.
    The ConfigMap contains shell scripts that are executed during VM customization.
    """

    configmap: ConfigMap
    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_configmap(
        self,
        test_configmap: ConfigMap | None,
    ) -> None:
        """Create virt-customize ConfigMap resource.

        Args:
            test_configmap: ConfigMap fixture that creates the virt-customize ConfigMap

        Raises:
            AssertionError: If ConfigMap creation fails or doesn't exist
        """
        self.__class__.configmap = test_configmap
        assert self.configmap, "ConfigMap fixture returned None"
        assert self.configmap.exists, "ConfigMap does not exist"
        assert self.configmap.name == "forklift-virt-customize", "ConfigMap name mismatch"

        # Verify the scripts are in the ConfigMap
        cm_data = self.configmap.instance.data
        assert "01_linux_firstboot_test.sh" in cm_data, "No firstboot script"
        assert "01_linux_run_test.sh" in cm_data, "No run script 1"

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource for migration.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for migration

        Returns:
            None

        Raises:
            AssertionError: If StorageMap creation fails
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for migration.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for migration
            multus_network_name: Name of the multus network

        Returns:
            None

        Raises:
            AssertionError: If NetworkMap creation fails
        """
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            multus_network_name=multus_network_name,
            vms=vms,
        )
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Create MTV Plan CR resource.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for migration
            source_provider_inventory: Source provider inventory

        Returns:
            None

        Raises:
            AssertionError: If Plan creation fails
        """
        populate_vm_ids(plan=prepared_plan, inventory=source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute warm migration with cutover.

        Args:
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            target_namespace: Target namespace for migration

        Returns:
            None
        """
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
            cut_over=get_cutover_value(),
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager",
    ) -> None:
        """Validate migrated VMs.

        Args:
            prepared_plan: The prepared migration plan
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_data: Source provider configuration data
            source_vms_namespace: Namespace of source VMs
            source_provider_inventory: Source provider inventory
            vm_ssh_connections: SSH connections to migrated VMs
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
