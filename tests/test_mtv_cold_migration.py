import pytest as pytest

from utilities.mtv_migration import get_network_migration_map, get_storage_migration_map, get_vm_suffix, migrate_vms
from utilities.utils import get_value_from_py_config

VM_SUFFIX = get_vm_suffix()


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": f"mtv-rhel8-sanity{VM_SUFFIX}", "guest_agent": True},
                    ],
                    "warm_migration": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["rhel8"],
)
@pytest.mark.tier0
def test_sanity_cold_mtv_migration(
    request,
    fixture_store,
    session_uuid,
    ocp_admin_client,
    mtv_namespace,
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    multus_network_name,
    source_provider_inventory,
):
    vms = [vm["name"] for vm in plans[0]["virtual_machines"]]
    storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        mtv_namespace=mtv_namespace,
        ocp_admin_client=ocp_admin_client,
        vms=vms,
    )
    network_migration_map = get_network_migration_map(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        mtv_namespace=mtv_namespace,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        vms=vms,
    )
    migrate_vms(
        fixture_store=fixture_store,
        test_name=request._pyfuncitem.name,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
    )


@pytest.mark.remote
@pytest.mark.parametrize(
    "plans",
    [
        # MTV-79
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": f"mtv-rhel8-79{VM_SUFFIX}"},
                        {
                            "name": f"mtv-win2019-79{VM_SUFFIX}",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
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
    session_uuid,
    ocp_admin_client,
    target_namespace,
    mtv_namespace,
    source_provider_inventory,
    plans,
    source_provider,
    source_provider_data,
    destination_ocp_provider,
    multus_network_name,
):
    vms = [vm["name"] for vm in plans[0]["virtual_machines"]]
    remote_storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        source_provider_inventory=source_provider_inventory,
        mtv_namespace=mtv_namespace,
        ocp_admin_client=ocp_admin_client,
        vms=vms,
    )
    remote_network_migration_map = get_network_migration_map(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        source_provider_inventory=source_provider_inventory,
        mtv_namespace=mtv_namespace,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        vms=vms,
    )
    migrate_vms(
        fixture_store=fixture_store,
        test_name=request._pyfuncitem.name,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        plans=plans,
        network_migration_map=remote_network_migration_map,
        storage_migration_map=remote_storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
    )


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": "1nisim-rhel9-efi", "guest_agent": True},
                    ],
                    "warm_migration": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["rhel9-efi.ova"],
)
@pytest.mark.ova
def test_ova_cold_mtv_migration(
    request,
    fixture_store,
    session_uuid,
    ocp_admin_client,
    mtv_namespace,
    multus_network_name,
    source_provider_inventory,
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
):
    vms = [vm["name"] for vm in plans[0]["virtual_machines"]]
    storage_migration_map = get_storage_migration_map(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        mtv_namespace=mtv_namespace,
        ocp_admin_client=ocp_admin_client,
        vms=vms,
    )
    network_migration_map = get_network_migration_map(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        mtv_namespace=mtv_namespace,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        vms=vms,
    )

    migrate_vms(
        fixture_store=fixture_store,
        test_name=request._pyfuncitem.name,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
        source_provider_inventory=source_provider_inventory,
    )
