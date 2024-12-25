import pytest
from pytest_testconfig import py_config

from utilities.mtv_migration import migrate_vms, get_cutover_value

if py_config["source_provider_type"] in ["openstack", "openshift"]:
    pytest.skip("OpenStack/OpenShift warm migration is not supported.", allow_module_level=True)

STORAGE_SUFFIX = ""
if py_config["matrix_test"]:
    SC = py_config["storage_class"]
    if "ceph-rbd" in SC:
        STORAGE_SUFFIX = "-ceph-rbd"
    elif "nfs" in SC:
        STORAGE_SUFFIX = "-nfs"


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
                            "name": f"mtv-rhel8-warm-sanity{STORAGE_SUFFIX}",
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
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    precopy_interval_forkliftcontroller,
    network_migration_map,
    storage_migration_map,
):
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
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
                            "name": f"mtv-rhel8-warm-2disks2nics{STORAGE_SUFFIX}",
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
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    precopy_interval_forkliftcontroller,
    network_migration_map,
    storage_migration_map,
):
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
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
                            "name": f"mtv-rhel8-warm-394{STORAGE_SUFFIX}",
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
@pytest.mark.skipif(not py_config.get("remote_ocp_cluster", False), reason="remote_ocp_cluster=false")
def test_warm_remote_ocp(
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_ocp_provider,
    precopy_interval_forkliftcontroller,
    remote_network_migration_map,
    remote_storage_migration_map,
):
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_ocp_provider,
        plans=plans,
        network_migration_map=remote_network_migration_map,
        storage_migration_map=remote_storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        target_namespace=target_namespace,
    )
