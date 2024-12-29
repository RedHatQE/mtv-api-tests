import pytest as pytest
from pytest_testconfig import py_config
from utilities.mtv_migration import get_vm_suffix, migrate_vms

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
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
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
@pytest.mark.skipif(not py_config.get("remote_ocp_cluster", False), reason="remote_ocp_cluster=false")
def test_cold_remote_ocp(
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_ocp_provider,
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
        target_namespace=target_namespace,
    )
