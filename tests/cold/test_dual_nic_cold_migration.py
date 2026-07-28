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
    """Validate Plan acceptance with dual NICs on same source network mapped to different NADs.

    Test scenario:
    1. Source VM on vSphere has 2 NICs both connected to the same source network (VM Network)
    2. First NIC maps to the pod network, one Multus NetworkAttachmentDefinition (NAD) is created
       on the OCP target cluster using cnv-bridge for the second NIC
    3. A NetworkMap is created with per-NIC mappings where the same source network is mapped to
       different destination NADs (one mapping per VM NIC)
    4. A migration Plan is created using the NetworkMap

    Expected result:
    - Forklift accepts the Plan without triggering VMDuplicateNADMappings validation error
    - Plan reaches Ready status, confirming that duplicate source network entries in the NetworkMap
      are valid when using per-NIC mapping

    This is a 3-step plan-readiness validation test (no migration executed).

    Jira: MTV-5623
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
