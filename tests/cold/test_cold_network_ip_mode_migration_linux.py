from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.guest_ip_mode import (
    verify_network_ip_modes,
    verify_network_map_network_ip_mode,
    verify_preserve_mode_dns_data,
)
from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms
from utilities.utils import populate_vm_ids, resolve_network_map_overrides

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
    [pytest.param(py_config["tests_params"]["test_cold_migration_network_ip_mode_linux"])],
    indirect=True,
    ids=["network-ip-mode-rhel9"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestColdNetworkIpModeLinuxMigration:
    """Cold-migrate a multi-NIC Linux VM with per-network guest IP configuration.

    Purpose/Regression:
        Verify the NetworkMap per-entry guest IP configuration (forklift
        MTV-5674) during a cold migration of a Linux VM with plan-level
        static IP preservation enabled: each mapped network selects its
        own guest IP behavior — preserve, none or DHCP — overriding the
        plan-level setting per network. Together with the Windows
        variant of this scenario, this covers every guest IP mode the
        NetworkMap offers. The DHCP-mode entry is a regression test for
        a reported MTV 5.0.0 bug: a statically configured NIC mapped
        with guest IP mode DHCP keeps its static address, gateway and
        DNS settings instead of being switched to DHCP configuration.
        A regression leaves a migrated Linux VM with the wrong
        per-interface IP configuration: preserved addresses lost,
        interfaces configured where no address was requested, or static
        settings kept where DHCP was requested.

    Prerequisites:
        Register and connect a VMware vSphere source provider and an OpenShift
        Virtualization destination provider. Prepare accessible destination
        storage and a Multus network attachment definition for each additional
        destination network. Use an MTV version that includes per-network
        guest IP configuration. Prepare a disposable, powered-on Linux VM with
        VMware Tools installed, SSH access enabled and a guest agent, with
        four NICs:

        - one on a network served by DHCP
        - two with a single static IPv4 address each, on different networks
        - one with multiple static IPv4 addresses (address, gateway and DNS set)

        Have permission to create maps and a Plan in the target namespace.

    Test plan:
        1. Start the source VM and wait until it appears in the provider
           inventory with guest network information.
        2. Create a StorageMap for the VM's disks. Create a NetworkMap that
           maps the DHCP network to the pod network without a guest IP mode,
           one single-static-IP network to a Multus network with guest IP mode
           none, the other single-static-IP network to a second Multus network
           with guest IP mode DHCP, and the multiple-static-IP network to a
           third Multus network with guest IP mode preserve. Wait for both
           maps to be ready and confirm every entry carries its configured
           destination and guest IP mode.
        3. Create a cold migration Plan with static IP preservation enabled
           and target power state on. Wait for the Plan ready condition.
        4. Run the migration and wait for it to succeed.
        5. Check the destination VM: every NIC is attached to the
           destination its NetworkMap entry selected. Check each mapped
           interface in the guest: the none-mode interface exists but
           carries no IPv4 address (it is not renamed to its source
           name); the DHCP-mode interface is DHCP configured
           (NetworkManager IPv4 method auto) with no retained source
           static address — the mapped Multus network serves no DHCP
           leases, so no address is expected on it, only the DHCP
           configuration itself; the preserve-mode interface kept its
           name, addresses, subnet mask, gateway and DNS servers.
           Confirm the VM is running with the expected disks and
           interfaces. The migrated VM, maps and Plan are then removed
           by the teardown.

    Expected result:
        1. The migration succeeds and the Plan reports Succeeded.
        2. Each NIC is attached to the destination its NetworkMap entry
           selected; no address for none mode, DHCP configuration for
           the DHCP-mode interface, and unchanged name, addresses,
           subnet mask, gateway and DNS servers for preserve mode. On
           failure, inspect the Plan and NetworkMap conditions, the
           VM's migration step and the interface's NetworkManager
           connection profile.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for migration

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
        assert self.__class__.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap with per-network destinations and guest IP modes.

        Resolves the plan's requirement-based network_map_overrides to the
        source VM's concrete networks (by guest IP configuration), then builds
        a NetworkMap where each network maps to its configured destination
        (pod or multus); entries with a guest IP mode carry the NetworkMap
        networkIPMode field (forklift MTV-5674). Asserts the live NetworkMap
        carries every configured destination and guest IP mode, so a cluster
        that prunes the field fails here instead of silently skipping the
        mode verification.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            source_provider_inventory: Source provider inventory
            target_namespace: Target namespace for migration
            multus_network_name: Multus network base name and namespace

        Raises:
            AssertionError: If NetworkMap creation fails or the created map
                does not carry the configured contract
        """
        network_map_overrides = resolve_network_map_overrides(
            network_map_overrides=prepared_plan["network_map_overrides"],
            source_vms_data=prepared_plan["source_vms_data"],
        )
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
            network_map_overrides=network_map_overrides,
        )
        assert self.__class__.network_map, "NetworkMap creation failed"
        verify_network_map_network_ip_mode(
            network_map_resource=self.__class__.network_map,
            network_map_overrides=network_map_overrides,
        )
        verify_preserve_mode_dns_data(
            network_map_overrides=network_map_overrides,
            source_vms_data=prepared_plan["source_vms_data"],
        )

    def test_create_plan(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR with cold migration and static IP preservation.

        Args:
            prepared_plan: The prepared migration plan
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            source_provider: Source provider instance
            destination_provider: Destination provider instance
            target_namespace: Target namespace for migration
            source_provider_inventory: Source provider inventory

        Raises:
            AssertionError: If Plan creation fails
        """
        populate_vm_ids(plan=prepared_plan, inventory=source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=source_provider,
            destination_provider=destination_provider,
            storage_map=self.__class__.storage_map,
            network_map=self.__class__.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan["warm_migration"],
            preserve_static_ips=prepared_plan["preserve_static_ips"],
            target_power_state=prepared_plan["target_power_state"],
        )
        assert self.__class__.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        target_namespace: str,
    ) -> None:
        """Execute cold migration.

        Args:
            fixture_store: Fixture store for resource tracking
            ocp_admin_client: OpenShift admin client
            target_namespace: Target namespace for migration
        """
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.__class__.plan_resource,
            target_namespace=target_namespace,
        )

    def test_check_vms(
        self,
        prepared_plan: dict[str, Any],
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        source_provider_data: dict[str, Any],
        source_vms_namespace: str,
        source_provider_inventory: ForkliftInventory,
        vm_ssh_connections: SSHConnectionManager,
    ) -> None:
        """Validate migrated VMs and per-network guest IP mode outcomes.

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
            network_map_resource=self.__class__.network_map,
            storage_map_resource=self.__class__.storage_map,
            source_provider_data=source_provider_data,
            source_vms_namespace=source_vms_namespace,
            source_provider_inventory=source_provider_inventory,
            vm_ssh_connections=vm_ssh_connections,
        )
        verify_network_ip_modes(
            plan=prepared_plan,
            network_map_resource=self.__class__.network_map,
            destination_provider=destination_provider,
            source_provider_data=source_provider_data,
            vm_ssh_connections=vm_ssh_connections,
        )
