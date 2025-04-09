import pytest
from pytest_testconfig import py_config

from utilities.mtv_migration import (
    create_storagemap_and_networkmap,
    get_cutover_value,
    get_vm_suffix,
    migrate_vms,
)
from utilities.utils import get_value_from_py_config

if py_config["source_provider_type"] in ["openstack", "openshift"]:
    pytest.skip("OpenStack/OpenShift warm migration is not supported.", allow_module_level=True)

VM_SUFFIX = get_vm_suffix()


@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": f"mtv-rhel8-warm-sanity{VM_SUFFIX}",
                            "source_vm_power": "on",
                            "guest_agent": True,
                        },
                    ],
                    "warm_migration": True,
                }
            ],
        ),
    ],
    indirect=True,
    ids=["rhel8"],
)
def test_sanity_warm_mtv_migration(
    request,
    fixture_store,
    session_uuid,
    ocp_admin_client,
    multus_network_name,
    source_provider_inventory,
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    precopy_interval_forkliftcontroller,
    ceph_clean,
):
    storage_migration_map, network_migration_map = create_storagemap_and_networkmap(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        plan=plans[0],
    )

    migrate_vms(
        request=request,
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
        ocp_admin_client=ocp_admin_client,
    )


@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": f"mtv-rhel8-warm-2disks2nics{VM_SUFFIX}",
                            "source_vm_power": "on",
                            "guest_agent": True,
                        },
                    ],
                    "warm_migration": True,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-200 rhel"],
)
def test_mtv_migration_warm_2disks2nics(
    request,
    fixture_store,
    session_uuid,
    ocp_admin_client,
    multus_network_name,
    source_provider_inventory,
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    precopy_interval_forkliftcontroller,
    ceph_clean,
):
    storage_migration_map, network_migration_map = create_storagemap_and_networkmap(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        plan=plans[0],
    )

    migrate_vms(
        request=request,
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
        ocp_admin_client=ocp_admin_client,
    )


@pytest.mark.remote
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": f"mtv-rhel8-warm-394{VM_SUFFIX}",
                            "source_vm_power": "on",
                            "guest_agent": True,
                        },
                    ],
                    "warm_migration": True,
                }
            ],
        ),
    ],
    indirect=True,
    ids=["MTV-394"],
)
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote OCP cluster provided")
def test_warm_remote_ocp(
    request,
    fixture_store,
    session_uuid,
    ocp_admin_client,
    multus_network_name,
    source_provider_inventory,
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_ocp_provider,
    precopy_interval_forkliftcontroller,
    ceph_clean,
):
    storage_migration_map, network_migration_map = create_storagemap_and_networkmap(
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        plan=plans[0],
    )

    migrate_vms(
        request=request,
        fixture_store=fixture_store,
        session_uuid=session_uuid,
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
        ocp_admin_client=ocp_admin_client,
    )
