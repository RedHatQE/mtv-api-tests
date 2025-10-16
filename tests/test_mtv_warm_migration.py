import pytest
from ocp_resources.provider import Provider
from pytest_testconfig import py_config

from utilities.migration_utils import get_cutover_value
from utilities.mtv_migration import (
    create_storagemap_and_networkmap,
    migrate_vms,
)
from utilities.utils import get_value_from_py_config

SOURCE_PROVIDER_TYPE = py_config.get("source_provider_type")


pytestmark = [
    pytest.mark.jira("MTV-2846", run=py_config["source_provider_type"] != Provider.ProviderType.RHV),
    pytest.mark.skipif(
        SOURCE_PROVIDER_TYPE
        in (Provider.ProviderType.OPENSTACK, Provider.ProviderType.OPENSHIFT, Provider.ProviderType.OVA),
        reason=f"{SOURCE_PROVIDER_TYPE} warm migration is not supported.",
    ),
]


@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_sanity_warm_mtv_migration"])],
    indirect=True,
    ids=["rhel8"],
)
def test_sanity_warm_mtv_migration(
    request,
    fixture_store,
    ocp_admin_client,
    multus_network_name,
    source_provider_inventory,
    target_namespace,
    destination_provider,
    plan,
    source_provider,
    source_provider_data,
    precopy_interval_forkliftcontroller,
    source_vms_namespace,
):
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
        ocp_admin_client=ocp_admin_client,
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
    )


@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_mtv_migration_warm_2disks2nics"])],
    indirect=True,
    ids=["MTV-200 rhel"],
)
def test_mtv_migration_warm_2disks2nics(
    request,
    fixture_store,
    ocp_admin_client,
    multus_network_name,
    source_provider_inventory,
    target_namespace,
    destination_provider,
    plan,
    source_provider,
    source_provider_data,
    precopy_interval_forkliftcontroller,
    source_vms_namespace,
):
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
        ocp_admin_client=ocp_admin_client,
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
    )


@pytest.mark.remote
@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_warm_remote_ocp"])],
    indirect=True,
    ids=["MTV-394"],
)
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote OCP cluster provided")
def test_warm_remote_ocp(
    request,
    fixture_store,
    ocp_admin_client,
    multus_network_name,
    source_provider_inventory,
    target_namespace,
    destination_ocp_provider,
    plan,
    source_provider,
    source_provider_data,
    precopy_interval_forkliftcontroller,
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
        ocp_admin_client=ocp_admin_client,
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
    )
