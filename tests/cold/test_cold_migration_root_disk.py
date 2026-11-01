from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms, get_vm_command_output
from utilities.utils import populate_vm_ids
from utilities.vmware_guest_operations import LinuxOSRoot, find_alternate_linux_os_root, parse_linux_os_release

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from libs.providers.vmware import VMWareProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_cold_migration_root_disk"], id="root-disk-cold")],
    indirect=True,
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestColdMigrationRootDiskSelection:
    """Cold-migrate a two-OS vSphere VM and select the non-booted Linux root.

    Purpose/Regression:
        Verify that a Plan's per-VM rootDisk setting selects the intended OS
        when the source VM has two bootable Linux installations. The test
        discovers the running and alternate OS identities and partition paths
        before Plan creation; booting the default OS instead fails validation.

    Prerequisites:
        Register and connect a vSphere source provider and an OpenShift
        destination with MTV installed. Provide accessible destination storage
        and network, and permission to create migration resources and a target
        namespace. Use an isolated, cloneable Linux VM with exactly two
        distinct Linux installations on supported single-letter /dev/sd
        partitions using ext or XFS filesystems.
        VMware Tools must be running and the configured Linux Guest Operations
        credentials must have root access to inspect the source clone. The test
        also needs SSH credentials for post-migration validation. It migrates a
        disposable clone and retains the reusable source VM.

    Test plan:
        1. Prepare and power on the source clone, then confirm it is visible in
           vSphere and Forklift inventory.
        2. Create StorageMap and NetworkMap resources. Through VMware Guest
           Operations, inspect the active root and read-only inspect other
           supported partitions for os-release. Set spec.vms[].rootDisk to the
           sole alternate OS partition and confirm the Plan contains it.
        3. Start the cold migration and confirm the Migration reaches Succeeded.
        4. Connect to the migrated VM over SSH and compare its OS ID and
           version with the alternate installation discovered on the source.
           Do not rely on the target root device retaining its source name.
        5. Run standard migrated-VM checks, then clean up the destination
           resources and namespace. Retain the reusable source VM.

    Expected result:
        The Plan stores the dynamically discovered alternate root path, the
        cold Migration succeeds, and the migrated VM reports the alternate
        source OS identity and passes standard VM validation. The target root
        device may be named differently after migration. On failure, inspect
        Plan and Migration conditions, the VM migration pipeline step, events,
        and the Guest Operations and SSH command output.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan
    selected_os_by_vm: dict[str, LinuxOSRoot]

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
        """Create the StorageMap for the source VM.

        Args:
            prepared_plan: Plan configuration with the prepared source VM.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: OpenShift destination provider.
            source_provider_inventory: Forklift inventory for the source VM.
            target_namespace: Namespace for migration resources.

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
        """Create the NetworkMap for the source VM.

        Args:
            prepared_plan: Plan configuration with the prepared source VM.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: OpenShift destination provider.
            source_provider_inventory: Forklift inventory for the source VM.
            target_namespace: Namespace for migration resources.
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
        assert self.network_map, "NetworkMap creation failed"

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "VMWareProvider",
        destination_provider: "OCPProvider",
        target_namespace: str,
        source_provider_inventory: "ForkliftInventory",
        source_provider_data: dict[str, Any],
    ) -> None:
        """Discover the alternate OS root, create a cold Plan, and confirm its rootDisk.

        Args:
            prepared_plan: Plan configuration with the source VM and settings.
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            source_provider: Registered vSphere source provider.
            destination_provider: OpenShift destination provider.
            target_namespace: Namespace for migration resources.
            source_provider_inventory: Forklift inventory containing the VM ID.
            source_provider_data: Provider configuration containing guest credentials.

        Raises:
            AssertionError: If the Plan does not contain the discovered root disk.
        """
        self.__class__.selected_os_by_vm = {}
        for vm in prepared_plan["virtual_machines"]:
            provider_vm = prepared_plan["source_vms_data"][vm["name"]]["provider_vm_api"]
            selected_os = find_alternate_linux_os_root(
                source_provider=source_provider,
                vm=provider_vm,
                source_provider_data=source_provider_data,
            )
            vm["rootDisk"] = selected_os["root_device"]
            self.__class__.selected_os_by_vm[vm["name"]] = selected_os

        populate_vm_ids(prepared_plan, source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            target_power_state=prepared_plan["target_power_state"],
            warm_migration=prepared_plan["warm_migration"],
        )
        for index, vm in enumerate(prepared_plan["virtual_machines"]):
            plan_vm = self.plan_resource.instance.spec.vms[index]
            assert plan_vm.rootDisk == vm["rootDisk"], (
                f"Plan VM rootDisk is '{plan_vm.rootDisk}', expected '{vm['rootDisk']}' for VM {vm['name']}"
            )

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        target_namespace: str,
    ) -> None:
        """Execute the cold migration.

        Args:
            fixture_store: Fixture store for resource tracking and cleanup.
            ocp_admin_client: OpenShift admin client.
            target_namespace: Namespace for the target VM.
        """
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
            target_namespace=target_namespace,
        )

    def test_verify_selected_root_os(
        self,
        prepared_plan: dict[str, Any],
        source_provider: "VMWareProvider",
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        vm_ssh_connections: "SSHConnectionManager",
    ) -> None:
        """Verify the migrated VM boots the discovered alternate OS installation.

        Args:
            prepared_plan: Plan configuration with the migrated VM.
            source_provider: Registered vSphere source provider.
            source_provider_data: Source provider credentials and settings.
            source_vms_namespace: Namespace used for source VM inventory.
            vm_ssh_connections: SSH connection manager for migrated VMs.
        """
        for vm in prepared_plan["virtual_machines"]:
            source_vm_info = source_provider.vm_dict(
                name=vm["name"],
                namespace=source_vms_namespace,
                source=True,
            )
            expected_os = self.selected_os_by_vm[vm["name"]]
            # rrmngmnt builds the SSH command string with subprocess.list2cmdline() (Windows
            # argv quoting), which does not protect "$". A quoted multi-word command referencing
            # a shell variable gets that variable expanded by the login shell before the inner
            # "bash -c" ever runs, so "$ID"/"$VERSION_ID" always resolve empty. Read the raw file
            # and parse it instead of relying on remote variable expansion or a loose substring
            # match, which could accept an unrelated field (e.g. BUILD_ID="rhel") or reject a
            # correct OS whose os-release uses unquoted/single-quoted values.
            raw_os_release = get_vm_command_output(
                vm=vm,
                vm_ssh_connections=vm_ssh_connections,
                source_vm_info=source_vm_info,
                source_provider_data=source_provider_data,
                command=["cat", "/etc/os-release"],
            )
            actual_os = parse_linux_os_release(raw_os_release)
            assert (actual_os["os_id"], actual_os["version_id"]) == (expected_os["os_id"], expected_os["version_id"]), (
                f"Migrated VM {vm['name']} booted {actual_os['os_id']} {actual_os['version_id']}, "
                f"expected the discovered alternate root {expected_os['os_id']} {expected_os['version_id']}"
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
        """Run standard checks on the migrated VM.

        Args:
            prepared_plan: Plan configuration with the migrated VM.
            source_provider: Registered vSphere source provider.
            destination_provider: OpenShift destination provider.
            source_provider_data: Source provider credentials and settings.
            source_vms_namespace: Namespace used for source VM inventory.
            source_provider_inventory: Forklift inventory for the source VM.
            vm_ssh_connections: SSH connection manager for migrated VMs.
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
