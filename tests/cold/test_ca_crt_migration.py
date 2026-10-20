from __future__ import annotations

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
from utilities.utils import get_value_from_py_config, populate_vm_ids

if TYPE_CHECKING:
    from kubernetes.dynamic import DynamicClient

    from libs.providers.openshift import OCPProvider


@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.openstack
@pytest.mark.esxi
@pytest.mark.ca_crt
@pytest.mark.tier1
@pytest.mark.incremental
@pytest.mark.skipif(
    get_value_from_py_config("source_provider_insecure_skip_verify"),
    reason="CA cert field test requires SSL verification enabled (insecure=false)",
)
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_ca_crt_cold_migration"],
        )
    ],
    indirect=True,
    ids=["MTV-4561-ca-crt"],
)
class TestCaCrtColdMigration:
    """Verify a provider secret that stores its CA certificate under ca.crt still connects and yields a Ready Plan (MTV-4561).

    Purpose/Regression:
        Forklift historically read the source provider CA certificate from a
        Secret field named ``cacert``. MTV-4561 added support for the standard
        Kubernetes convention ``ca.crt``. The failure path exercised here is
        provider connectivity with TLS verification enabled: the CA-cert code
        path runs during provider creation, so a Plan that reaches Ready proves
        the ``ca.crt`` field was accepted. No migration is executed.

    Prerequisites:
        Register an OpenShift destination with MTV installed and accessible
        destination storage and network. Have a source provider whose endpoint
        is secured by a CA certificate you can supply, and register it with SSL
        verification enabled (insecure skip-verify disabled) while storing the
        CA certificate in the provider Secret under the key ``ca.crt`` rather
        than ``cacert``. Use a disposable Linux VM with a disk and network
        interface supported by both providers, and guest-agent access. Have
        permission to create maps and a Plan in a target namespace.

    Test plan:
        1. Inspect the provider Secret referenced by the registered source
           provider and confirm its data contains the key ``ca.crt`` and does
           not contain the legacy key ``cacert``.
        2. Create a StorageMap that maps the VM's disks to destination
           storage, and confirm it is created.
        3. Create a NetworkMap that maps the VM's network to a destination
           network, and confirm it is created.
        4. Create a cold-migration Plan for the VM using both maps, and check
           that the Plan's Ready condition becomes True. This proves the
           provider authenticated through the ``ca.crt`` Secret field.

    Expected result:
        The provider Secret contains only ``ca.crt`` (not ``cacert``), and the
        Plan reaches Ready. On failure, inspect the provider resource's
        conditions for connection or authentication errors, confirm the
        Secret key name and certificate contents, and review the forklift
        controller pod logs for TLS handshake errors against the source
        endpoint.
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    @pytest.mark.usefixtures("prepared_plan")
    def test_verify_ca_crt_secret(
        self,
        ca_crt_source_provider: BaseProvider,
        ocp_admin_client: DynamicClient,
    ) -> None:
        """Verify the provider secret uses ca.crt field instead of cacert."""
        assert ca_crt_source_provider.ocp_resource is not None, "ocp_resource is not set"
        secret_ref = ca_crt_source_provider.ocp_resource.instance.spec.secret
        secret = Secret(
            client=ocp_admin_client,
            name=secret_ref.name,
            namespace=secret_ref.namespace,
        )
        secret_data_keys = list(secret.instance.data.keys())
        assert "ca.crt" in secret_data_keys, f"Expected 'ca.crt' in secret keys, got: {secret_data_keys}"
        assert "cacert" not in secret_data_keys, (
            f"Legacy 'cacert' should not be in secret keys, got: {secret_data_keys}"
        )

    def test_create_storagemap(
        self,
        prepared_plan: dict[str, Any],
        fixture_store: dict[str, Any],
        ocp_admin_client: DynamicClient,
        ca_crt_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        ca_crt_source_provider_inventory: ForkliftInventory,
        target_namespace: str,
    ) -> None:
        """Create StorageMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.storage_map = get_storage_migration_map(
            fixture_store=fixture_store,
            source_provider=ca_crt_source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=ca_crt_source_provider_inventory,
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
        ca_crt_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        ca_crt_source_provider_inventory: ForkliftInventory,
        target_namespace: str,
        multus_network_name: dict[str, str],
    ) -> None:
        """Create NetworkMap resource for migration."""
        vms = [vm["name"] for vm in prepared_plan["virtual_machines"]]
        self.__class__.network_map = get_network_migration_map(
            fixture_store=fixture_store,
            source_provider=ca_crt_source_provider,
            destination_provider=destination_provider,
            source_provider_inventory=ca_crt_source_provider_inventory,
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
        ca_crt_source_provider: BaseProvider,
        destination_provider: OCPProvider,
        target_namespace: str,
        ca_crt_source_provider_inventory: ForkliftInventory,
    ) -> None:
        """Create MTV Plan CR resource."""
        populate_vm_ids(prepared_plan, ca_crt_source_provider_inventory)

        self.__class__.plan_resource = create_plan_resource(
            ocp_admin_client=ocp_admin_client,
            fixture_store=fixture_store,
            source_provider=ca_crt_source_provider,
            destination_provider=destination_provider,
            storage_map=self.storage_map,
            network_map=self.network_map,
            virtual_machines_list=prepared_plan["virtual_machines"],
            target_namespace=target_namespace,
            warm_migration=prepared_plan.get("warm_migration", False),
        )
        assert self.plan_resource, "Plan creation failed"
