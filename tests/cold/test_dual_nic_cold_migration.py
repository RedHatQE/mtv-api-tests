from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from utilities.mtv_migration import (
    create_plan_resource,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient
    from libs.base_provider import BaseProvider
    from libs.forklift_inventory import ForkliftInventory
    from libs.providers.openshift import OCPProvider


@pytest.mark.vsphere
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_cold_dual_nic_same_network_migration"],
        )
    ],
    indirect=True,
    ids=["MTV-692"],
)
class TestColdDualNicSameNetworkPlanValidation:
    """A Plan whose NetworkMap maps two NICs on the same source network to different destination networks must be accepted (MTV-5623).

    Purpose/Regression:
        With per-NIC network mapping, the same source network can legitimately
        appear multiple times in a NetworkMap — once per VM NIC — each entry
        mapped to a different destination network. Regression MTV-5623 guards
        against Forklift rejecting such a Plan with a
        VMDuplicateNADMappings validation error. The failure path is Plan
        admission: the Plan must reach Ready without a validation error. No
        migration is executed.

    Prerequisites:
        Register and connect a vSphere source provider and an OpenShift
        destination with MTV installed. Use a disposable Linux VM with two
        NICs, both connected to the same source network (e.g. ``VM Network``).
        On the destination, prepare two reachable networks for the two NICs:
        the pod network and a secondary NetworkAttachmentDefinition (e.g. a
        Linux bridge NAD). Have permission to create maps and a Plan in a
        target namespace.

    Test plan:
        1. Prepare the VM with both NICs on the same source network and wait
           until it appears in Forklift inventory. Prepare the destination
           networks, including a secondary NetworkAttachmentDefinition for the
           second NIC.
        2. Create a StorageMap for the VM's disks. Confirm it is created.
        3. Create a NetworkMap with per-NIC mappings: the source network
           appears twice, once per VM NIC, each mapped to a different
           destination network. Confirm the NetworkMap is created.
        4. Create a Plan for the VM using the maps, and check that it is
           accepted and its Ready condition becomes True, with no
           VMDuplicateNADMappings validation error. After the check, the
           maps and Plan are removed with the run.

    Expected result:
        The Plan referencing the per-NIC NetworkMap with duplicate source
        network entries is admitted and reaches Ready. On failure, inspect
        the Plan's conditions for a validation error naming duplicate NAD
        mappings and confirm each NetworkMap entry maps one VM NIC to one
        destination network.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: "DynamicClient",
        source_provider: "BaseProvider",
        destination_provider: "BaseProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource.

        Expected result: StorageMap is created successfully and stored as class attribute.
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
        destination_provider: "BaseProvider",
        source_provider_inventory: "ForkliftInventory",
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap with per-NIC mappings allowing duplicate source network entries.

        Creates a NetworkMap where both VM NICs (connected to the same source network) are mapped
        to different destination NADs using per-NIC mapping (`per_nic_network_map=True`).

        Expected result: NetworkMap is created with duplicate source network entries (same source
        network appears multiple times, once per VM NIC, each mapped to a different destination NAD).
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
            per_nic_network_map=prepared_plan.get("per_nic_network_map", False),
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
        """Create Plan using NetworkMap with duplicate source network entries.

        Creates an MTV Plan CR referencing the NetworkMap with duplicate source network entries
        (same source network mapped to different NADs via per-NIC mapping).

        Expected result: Forklift accepts the Plan and it reaches Ready status without triggering
        VMDuplicateNADMappings validation error, confirming that duplicate source network entries
        are valid when using per-NIC network mapping.
        """
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
            warm_migration=prepared_plan.get("warm_migration", False),
        )
        assert self.plan_resource, "Plan creation failed"
