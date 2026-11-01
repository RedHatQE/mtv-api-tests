from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.pod import Pod
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from exceptions.exceptions import MigrationNotFoundError
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_migration_for_plan,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.naming import resolve_destination_vm_name
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
    from ocp_resources.virtual_machine import VirtualMachine

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
        ),
        pytest.param(
            py_config["tests_params"]["test_cold_migration_skip_guest_conversion_win"],
            id="skip-guest-conversion-cold-win",
        ),
    ],
    indirect=True,
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestColdMigrationSkipGuestConversion:
    """Cold-migrate vSphere VMs (RHEL and Windows) with guest conversion skipped.

    Purpose/Regression:
        Verify no Plan-labeled virt-v2v pod is found at sampled migration status
        polls while migration still produces a usable VM. The Plan API default
        useCompatibilityMode=true must be preserved. The Windows param additionally
        exercises the WindowsWaitForGuestReboot pipeline stage being skipped
        (forklift gates it on "!SkipGuestConversion", pkg/controller/plan/migrator/
        base/migrator.go) — Windows raw copy is the primary customer scenario for
        skipGuestConversion.

    Prerequisites:
        Register vSphere source and OpenShift Virtualization destination providers
        on a healthy MTV cluster. Provide accessible destination storage and
        network, plus permission to create migration resources and target-namespace
        NADs. Use an isolated, disposable, powered-on VM with a boot disk, NIC, guest
        agent, and SSH access (RHEL 8 param), or a powered-on Windows Server 2019
        VM with a boot disk, NIC, and Windows guest credentials in the provider
        config (guest_vm_win_user/guest_vm_win_password; Windows param). For extra source networks, provide
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
           Verify the compatibility-mode device models on the destination VM
           (sata disks, e1000e interfaces, usb tablet input) and the
           guestConverted="false" label. Delete the Migration, Plan, maps,
           destination VM, temporary NADs, and target namespace.

    Expected result:
        The Plan has skipGuestConversion=true and useCompatibilityMode=true;
        migration succeeds with no Plan-labeled virt-v2v pod found at sampled status
        polls, and the destination VM passes power, guest-agent, and SSH checks. The
        destination VM uses compatibility-mode devices (disk.bus "sata", interface
        model "e1000e", tablet input bus "usb") and carries label
        guestConverted="false". On failure, inspect Plan and Migration conditions,
        the failed VM migration step, events, and Plan-labeled pods before cleanup.
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

            Conversion pods are selected by both the Plan and the Migration labels,
            matching the selectors used by the rest of the suite (see
            utilities/resume_conversion.py, utilities/copyoffload_migration.py),
            so a virt-v2v pod carrying only one of the labels is still detected.

            Args:
                _status: Current Plan migration status sample.

            Raises:
                AssertionError: If a guest-conversion pod is found.
            """
            label_selectors = [f"forklift.app=virt-v2v,plan={plan_uid}"]
            try:
                migration_uid = get_migration_for_plan(plan=self.__class__.plan_resource).instance.metadata.uid
                label_selectors.append(f"forklift.app=virt-v2v,migration={migration_uid}")
            except MigrationNotFoundError:
                # No Migration CR exists yet, so no migration-labeled pod can exist either.
                pass

            conversion_pods = [
                pod
                for label_selector in label_selectors
                for pod in Pod.get(
                    client=ocp_admin_client,
                    namespace=plan_namespace,
                    label_selector=label_selector,
                )
            ]
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

        Raises:
            AssertionError: If a migrated VM fails post-migration, device-model, or
                guestConverted-label checks.
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

        # check_vms validated the guest; re-fetch the same destination VM dict (via
        # the shared OCPProvider.vm_dict helper) to inspect the KubeVirt spec and
        # labels set by the Forklift skipGuestConversion contract.
        for vm in prepared_plan["virtual_machines"]:
            destination_vm = destination_provider.vm_dict(
                name=resolve_destination_vm_name(vm),
                namespace=prepared_plan["_vm_target_namespace"],
            )
            self._check_destination_vm_device_models(destination_vm=destination_vm)
            self._check_guest_converted_label(destination_vm=destination_vm)

    def _check_destination_vm_device_models(self, destination_vm: ProviderData) -> None:
        """Verify compatibility-mode device models on the migrated destination VM.

        With skipGuestConversion=true (and useCompatibilityMode=true), Forklift's
        vSphere builder emits source-compatible devices instead of virtio: every
        disk on the sata bus, every interface with model e1000e, and a tablet
        input device on the usb bus.

        Args:
            destination_vm: Destination VM dict from OCPProvider.vm_dict; its
                "provider_vm_api" entry is the KubeVirt VirtualMachine resource.

        Raises:
            AssertionError: If any disk bus, interface model, or input bus differs
                from the compatibility-mode contract, or a device list is empty.
        """
        vm_name: str = destination_vm["name"]
        vm_resource: "VirtualMachine" = destination_vm["provider_vm_api"]
        devices: dict[str, Any] = vm_resource.instance.spec.template.spec.domain.devices

        disks: list[dict[str, Any]] = devices.get("disks") or []
        assert disks, f"Destination VM '{vm_name}' has no disks in its spec"
        disk_buses = [disk.get("disk", {}).get("bus") for disk in disks]
        assert all(bus == "sata" for bus in disk_buses), (
            f"Destination VM '{vm_name}' expected all disks on bus 'sata', got {disk_buses}"
        )

        interfaces: list[dict[str, Any]] = devices.get("interfaces") or []
        assert interfaces, f"Destination VM '{vm_name}' has no interfaces in its spec"
        interface_models = [interface.get("model") for interface in interfaces]
        assert all(model == "e1000e" for model in interface_models), (
            f"Destination VM '{vm_name}' expected all interfaces with model 'e1000e', got {interface_models}"
        )

        inputs: list[dict[str, Any]] = devices.get("inputs") or []
        assert inputs, f"Destination VM '{vm_name}' has no input devices in its spec"
        input_buses = [input_device.get("bus") for input_device in inputs]
        assert all(bus == "usb" for bus in input_buses), (
            f"Destination VM '{vm_name}' expected all input devices on bus 'usb', got {input_buses}"
        )

    def _check_guest_converted_label(self, destination_vm: ProviderData) -> None:
        """Verify the guestConverted label on the migrated destination VM.

        Forklift labels each destination VM "guestConverted" with the inverse of
        the Plan's skipGuestConversion, so a skipped-conversion migration must
        carry guestConverted="false" on the VirtualMachine metadata.

        Args:
            destination_vm: Destination VM dict from OCPProvider.vm_dict; its
                "provider_vm_api" entry is the KubeVirt VirtualMachine resource.

        Raises:
            AssertionError: If the guestConverted label is missing or not "false".
        """
        vm_name: str = destination_vm["name"]
        vm_resource: "VirtualMachine" = destination_vm["provider_vm_api"]
        labels: dict[str, Any] = vm_resource.instance.metadata.labels or {}
        assert labels.get("guestConverted") == "false", (
            f"Destination VM '{vm_name}' expected label guestConverted='false', got "
            f"{labels.get('guestConverted')!r} (labels: {labels})"
        )
