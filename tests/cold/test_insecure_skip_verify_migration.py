from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import pytest
from ocp_resources.network_map import NetworkMap
from ocp_resources.plan import Plan
from ocp_resources.secret import Secret
from ocp_resources.storage_map import StorageMap
from pytest_testconfig import config as py_config

from libs.base_provider import BaseProvider
from libs.forklift_inventory import ForkliftInventory
from utilities.mtv_migration import (
    create_plan_resource,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.utils import populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.providers.openshift import OCPProvider


@pytest.mark.vsphere
@pytest.mark.openstack
@pytest.mark.esxi
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_insecure_skip_verify_cold_migration"],
        )
    ],
    indirect=True,
    ids=["MTV-664"],
)
class TestInsecureSkipVerifyColdMigration:
    """Verify provider with insecureSkipVerify=true reaches plan readiness.

    Test scenario:
    1. A source provider is created with insecureSkipVerify=true in its Secret, bypassing
       TLS certificate verification for the provider connection
    2. The provider secret is verified to contain insecureSkipVerify set to "true"
    3. StorageMap and NetworkMap resources are created using the insecure provider
    4. A migration Plan is created using the insecure provider's StorageMap and NetworkMap

    Expected result:
    - Provider connects successfully to the source infrastructure with TLS verification disabled
    - Plan reaches Ready status, confirming that the insecureSkipVerify flag is properly
      propagated and honored by Forklift

    This is a 4-step plan-readiness validation test (no migration executed).
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    @pytest.mark.usefixtures("prepared_plan")
    def test_verify_insecure_skip_verify(
        self,
        insecure_source_provider: BaseProvider,
        ocp_admin_client: DynamicClient,
    ) -> None:
        """Verify the provider secret has insecureSkipVerify set to true."""
        assert insecure_source_provider.ocp_resource is not None, "ocp_resource is not set"
        secret_ref = insecure_source_provider.ocp_resource.instance.spec.secret
        secret = Secret(
            client=ocp_admin_client,
            name=secret_ref.name,
            namespace=secret_ref.namespace,
        )
        actual_value = base64.b64decode(secret.instance.data["insecureSkipVerify"]).decode("utf-8")
        assert actual_value == "true", f"Expected insecureSkipVerify='true', got '{actual_value}'"

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        insecure_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        insecure_source_provider_inventory: ForkliftInventory,
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=insecure_source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=insecure_source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        insecure_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        insecure_source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=insecure_source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=insecure_source_provider_inventory,
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
        insecure_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        insecure_source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        populate_vm_ids(prepared_plan, insecure_source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=insecure_source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
        )
        assert self.plan_resource, "Plan creation failed"


@pytest.mark.rhv
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_insecure_skip_verify_cold_migration_rhv"],
        )
    ],
    indirect=True,
    ids=["MTV-664-rhv"],
)
class TestInsecureSkipVerifyRhvColdMigration:
    """Verify provider with insecureSkipVerify=true reaches plan readiness (RHV).

    RHV variant without skip_clone — RHV uses template names that require cloning
    to produce VMs visible in the Forklift inventory.

    Test scenario:
    1. A source provider is created with insecureSkipVerify=true in its Secret, bypassing
       TLS certificate verification for the provider connection
    2. The provider secret is verified to contain insecureSkipVerify set to "true"
    3. StorageMap and NetworkMap resources are created using the insecure provider
    4. A migration Plan is created using the insecure provider's StorageMap and NetworkMap

    Expected result:
    - Provider connects successfully to the source infrastructure with TLS verification disabled
    - Plan reaches Ready status, confirming that the insecureSkipVerify flag is properly
      propagated and honored by Forklift

    This is a 4-step plan-readiness validation test (no migration executed).
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    @pytest.mark.usefixtures("prepared_plan")
    def test_verify_insecure_skip_verify(
        self,
        insecure_source_provider: BaseProvider,
        ocp_admin_client: DynamicClient,
    ) -> None:
        """Verify the provider secret has insecureSkipVerify set to true."""
        assert insecure_source_provider.ocp_resource is not None, "ocp_resource is not set"
        secret_ref = insecure_source_provider.ocp_resource.instance.spec.secret
        secret = Secret(
            client=ocp_admin_client,
            name=secret_ref.name,
            namespace=secret_ref.namespace,
        )
        actual_value = base64.b64decode(secret.instance.data["insecureSkipVerify"]).decode("utf-8")
        assert actual_value == "true", f"Expected insecureSkipVerify='true', got '{actual_value}'"

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        insecure_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        insecure_source_provider_inventory: ForkliftInventory,
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=insecure_source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=insecure_source_provider_inventory,
            ocp_admin_client=ocp_admin_client,
            target_namespace=target_namespace,
            vms=vms,
        )
        assert self.storage_map, "StorageMap creation failed"

    def test_create_networkmap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        insecure_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        insecure_source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=insecure_source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=insecure_source_provider_inventory,
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
        insecure_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        insecure_source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        populate_vm_ids(prepared_plan, insecure_source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=insecure_source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
        )
        assert self.plan_resource, "Plan creation failed"
