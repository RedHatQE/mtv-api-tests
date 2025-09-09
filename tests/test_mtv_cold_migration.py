import pytest
from ocp_resources.provider import Provider
from pytest_testconfig import config as py_config

from utilities.mtv_migration import (
    create_storagemap_and_networkmap,
    migrate_vms,
)
from utilities.utils import get_value_from_py_config


@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_sanity_cold_mtv_migration"])],
    indirect=True,
    ids=["rhel8"],
)
@pytest.mark.tier0
def test_sanity_cold_mtv_migration(
    request,
    fixture_store,
    ocp_admin_client,
    target_namespace,
    destination_provider,
    plan,
    source_provider,
    source_provider_data,
    multus_network_name,
    source_provider_inventory,
    source_vms_namespace,
):
    if source_provider.type == Provider.ProviderType.OVA:
        plan["virtual_machines"] = [
            {"name": "1nisim-rhel9-efi"},
        ]

    storage_migration_map, network_migration_map = create_storagemap_and_networkmap(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        plan=plan,
    )

    migrate_vms(
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
    )


@pytest.mark.remote
@pytest.mark.parametrize(
    "plan",
    [
        # MTV-79
        pytest.param(
            {
                "virtual_machines": [
                    {"name": "mtv-rhel8-79"},
                    {
                        "name": "mtv-win2019-79",
                    },
                ],
                "warm_migration": False,
            }
            # TODO fix Polarion ID
        )
    ],
    indirect=True,
    ids=["MTV-79"],
)
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote OCP cluster provided")
def test_cold_remote_ocp(
    request,
    fixture_store,
    ocp_admin_client,
    target_namespace,
    source_provider_inventory,
    destination_ocp_provider,
    plan,
    source_provider,
    source_provider_data,
    multus_network_name,
    source_vms_namespace,
):
    storage_migration_map, network_migration_map = create_storagemap_and_networkmap(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        plan=plan,
    )

    migrate_vms(
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
    )
