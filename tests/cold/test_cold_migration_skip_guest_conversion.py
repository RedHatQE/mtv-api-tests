from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms
from utilities.utils import populate_vm_ids

# These fixture contracts intentionally use Any because prepared_plan is composed from
# scenario-specific configuration and provider-derived VM data.
PreparedPlan = dict[str, Any]

# pytest-harvest stores metadata and cleanup records for different resource types.
FixtureStore = dict[str, Any]

# Provider configuration keys differ by provider type and providers_schema.json.
ProviderData = dict[str, Any]


if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_cold_migration_skip_guest_conversion"],
            id="skip-guest-conversion-cold",
        )
    ],
    indirect=True,
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestColdMigrationSkipGuestConversion:
    """Cold-migrate a vSphere RHEL VM with guest conversion skipped.

    Purpose/Regression:
        Verify no Plan-labeled virt-v2v pod is found at sampled migration status
        polls while migration still produces a usable VM. The Plan API default
        useCompatibilityMode=true must be preserved.

    Prerequisites:
        Register vSphere source and OpenShift Virtualization destination providers
        on a healthy MTV cluster. Provide accessible destination storage and
        network, plus permission to create migration resources and target-namespace
        NADs. Use an isolated, disposable, powered-on RHEL 8 VM with a boot disk,
        NIC, guest agent, and SSH access. For extra source networks, provide
        matching NADs in the target namespace. An equivalent guest is acceptable;
        do not use a shared or production VM.

    Test plan:
        1. Confirm providers are Ready and storage and network are accessible.
           Prepare the powered-on VM and wait for Forklift inventory; create an
           isolated target namespace and confirm it is Active.
        2. Create StorageMap and NetworkMap for the VM. Confirm both are created
           and extra-network destinations use the target namespace.
        3. Create a cold Plan with skipGuestConversion=true and target power on.
           Leave useCompatibilityMode unset and confirm Ready=True within 360 seconds.
        4. Start the migration; confirm Succeeded within 3600 seconds and no
           Plan-labeled virt-v2v pod is present at any status poll.
        5. Confirm the VM is powered on, the guest agent responds, and SSH works.
           Delete the Migration, Plan, maps, destination VM, temporary NADs, and
           target namespace.

    Expected result:
        The Plan has skipGuestConversion=true and useCompatibilityMode=true;
        migration succeeds with no Plan-labeled virt-v2v pod found at sampled status
        polls, and the destination VM passes power, guest-agent, and SSH checks. On
        failure, inspect Plan and Migration conditions, the failed VM migration
        step, events, and Plan-labeled pods before cleanup.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: PreparedPlan,
        fixture_store: FixtureStore,
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
    ) -> None:
        """Create the StorageMap for the source VM.

        Args:
            prepared_plan: Plan configuration with the source VM details.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: Registered OpenShift destination provider.
            source_provider_inventory: Inventory containing the source VM.
            target_namespace: Namespace for the migration resources.

        Raises:
            AssertionError: If StorageMap creation fails.
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
        assert self.__class__.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: PreparedPlan,
        fixture_store: FixtureStore,
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create the NetworkMap for the source VM.

        Args:
            prepared_plan: Plan configuration with the source VM details.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: Registered OpenShift destination provider.
            source_provider_inventory: Inventory containing the source VM.
            target_namespace: Namespace for the migration resources.
            multus_network_name: Optional additional network name and namespace.

        Raises:
            AssertionError: If NetworkMap creation fails.
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
        assert self.__class__.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: PreparedPlan,
        fixture_store: FixtureStore,
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
    ) -> None:
        """Create a cold Plan with guest conversion skipped.

        Args:
            prepared_plan: Plan configuration with the source VM and Plan settings.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: Registered OpenShift destination provider.
            target_namespace: Namespace for the migration resources.
            source_provider_inventory: Inventory containing the source VM ID.

        Raises:
            AssertionError: If the Plan is not created with guest conversion skipped.
        """
        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.__class__.storage_map,
            network_map=self.__class__.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            target_power_state=prepared_plan["target_power_state"],
            warm_migration=prepared_plan["warm_migration"],
            additional_plan_options={"skip_guest_conversion": prepared_plan["skip_guest_conversion"]},
        )
        assert self.__class__.plan_resource, "Plan creation failed"
        assert self.__class__.plan_resource.instance.spec.skipGuestConversion is True
        assert self.__class__.plan_resource.instance.spec.useCompatibilityMode is True

    def test_migrate_vms(
        self,
        fixture_store: FixtureStore,
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Migrate the VM and check for a virt-v2v pod at each status poll.

        Args:
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client for checking Plan pods.
            target_namespace: Namespace for the Migration resource.

        Raises:
            AssertionError: If a guest-conversion pod is present at a status poll.
            MigrationPlanExecError: If the migration fails or times out.
        """
        plan_uid = self.__class__.plan_resource.instance.metadata.uid
        plan_namespace = self.__class__.plan_resource.namespace

        def assert_no_guest_conversion_pod(_status: str) -> None:
            """Assert this Plan has no guest-conversion pod at the current poll.

            Args:
                _status: Current Plan migration status sample.

            Raises:
                AssertionError: If a guest-conversion pod is found.
            """
            conversion_pods = list(
                Pod.get(
                    client=ocp_admin_client,
                    namespace=plan_namespace,
                    label_selector=f"forklift.app=virt-v2v,plan={plan_uid}",
                )
            )
            assert not conversion_pods, (
                f"Guest conversion pod(s) present at a migration status poll for Plan '{self.__class__.plan_resource.name}': "
                f"{[pod.name for pod in conversion_pods]}"
            )

        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.__class__.plan_resource,
            target_namespace=target_namespace,
            on_status_poll=assert_no_guest_conversion_pod,
        )

    def test_check_vms(
        self,
        prepared_plan: PreparedPlan,
        source_provider: "BaseProvider",
        destination_provider: "OCPProvider",
        source_provider_data: ProviderData,
        source_vms_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        vm_ssh_connections: "SSHConnectionManager",
    ) -> None:
        """Validate the migrated VM, guest agent, and SSH connectivity.

        Args:
            prepared_plan: Plan configuration with target power and guest-agent settings.
            source_provider: Registered vSphere source provider.
            destination_provider: Registered OpenShift destination provider.
            source_provider_data: Source provider connection configuration.
            source_vms_namespace: Namespace used for source VM inventory lookups.
            source_provider_inventory: Inventory containing the source VM.
            vm_ssh_connections: SSH connection manager for the migrated VM.
        """
        check_vms(
            plan=prepared_plan,
            source_provider=source_provider,
            destination_provider=destination_provider,
            network_map_resource=self.__class__.network_map,
            storage_map_resource=self.__class__.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
