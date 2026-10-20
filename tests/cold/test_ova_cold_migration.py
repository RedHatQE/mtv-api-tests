from __future__ import annotations

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
from utilities.post_migration import check_vms
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider
    from utilities.ssh_utils import SSHConnectionManager


@pytest.mark.tier0
@pytest.mark.ova
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_ova_cold_migration"],
        )
    ],
    indirect=True,
    ids=["ova-cold"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestOvaColdMigration:
    """Cold-migrate a VM imported from an OVA file and verify it runs on the destination with the configured labels and affinity.

    Purpose/Regression:
        OVA is a file-based source with no live source inventory stats, so
        the scenario verifies the destination side only: the VM must be
        created from the OVA, powered on per the Plan, reachable, and carry
        the configured target labels and affinity rules. There is no induced
        failure.

    Prerequisites:
        Register and connect an OVA source provider (an accessible OVA file
        containing a Windows or Linux VM with several disks) and an OpenShift
        destination with MTV installed. Prepare destination storage and a
        network reachable by the migrated VM (e.g. the pod network or a
        NetworkAttachmentDefinition). Have permission to create maps, a Plan,
        and a target namespace, and SSH or equivalent access to the migrated
        VM for connectivity checks.

    Test plan:
        1. Make the OVA VM visible in Forklift inventory through the OVA
           provider.
        2. Create a StorageMap for the VM's disks and a NetworkMap for its
           network. Confirm both are created.
        3. Create a cold Plan for the VM with target power state on, target
           VM labels, and a pod-affinity rule. Check the Plan's Ready
           condition is True.
        4. Run the migration and check it reaches Succeeded.
        5. Validate the migrated VM: it is powered on, guest connectivity
           works, and the target labels and affinity rule are present on the
           VM. (Source-side equivalence checks do not apply to OVA sources.)
           After the checks, delete the migrated VM; the maps and Plan are
           removed with the run.

    Expected result:
        The OVA VM is migrated and powered on with working connectivity, and
        its metadata carries the configured target labels and affinity. On
        failure, inspect the migration CR status for the VM's failed step, the
        VM's spec and events in the target namespace, and the OVA provider
        resource's conditions.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
    ):
        """Create StorageMap resource for migration."""
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
        prepared_plan,
        fixture_store,
        ocp_admin_client,
        source_provider,
        destination_provider,
        source_provider_inventory,
        target_namespace,
        multus_network_name,
    ):
        """Create NetworkMap resource for migration."""
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
        ocp_admin_client: DynamicClient,
        source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        source_provider_inventory: ForkliftInventory,
        target_vm_labels: dict[str, Any],
    ) -> None:
        """Create MTV Plan CR resource."""
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
            warm_migration=prepared_plan["warm_migration"],
            target_power_state=prepared_plan["target_power_state"],
            target_labels=target_vm_labels["vm_labels"],
            target_affinity=prepared_plan["target_affinity"],
        )
        assert self.plan_resource, "Plan creation failed"

    def test_migrate_vms(
        self,
        fixture_store,
        ocp_admin_client,
        target_namespace,
    ):
        """Execute migration."""
        execute_migration(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            plan=self.plan_resource,
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
        vm_ssh_connections: SSHConnectionManager | None,
        target_vm_labels: dict[str, Any],
    ) -> None:
        """Validate migrated VMs."""
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
            target_vm_labels=target_vm_labels,
        )
