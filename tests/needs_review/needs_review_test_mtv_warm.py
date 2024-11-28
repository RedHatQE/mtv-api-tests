import pytest
from pytest_testconfig import config

from ocp_resources.mtv import MTV
from utilities.mtv_migration import migrate_vms, get_cutover_value

if config["source_provider_type"] in ["openstack", "openshift"]:
    pytest.skip("OpenStack/OpenShift warm migration is not supported.", allow_module_level=True)

STORAGE_SUFFIX = ""
if config["matrix_test"]:
    SC = config["storage_class"]
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
    )


@pytest.mark.tier1
@pytest.mark.warm
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": "mtv-rhel8-warm-201", "source_vm_power": "off"},
                    ],
                    "warm_migration": True,
                }
            ],
        ),
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": "mtv-rhel8-warm-204", "source_vm_power": "on"},
                    ],
                    "warm_migration": True,
                    "current_cutover": True,
                }
            ],
        ),
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": "mtv-rhel8-warm-206", "source_vm_power": "on"},
                    ],
                    "warm_migration": True,
                }
            ],
        ),
    ],
    indirect=True,
    ids=["MTV-201 shutdown_rhel_vm", "MTV-204 current_cutover", "MTV-206 rhel_vm_with_snapshots"],
)
def test_mtv_warm_p1(
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
        cut_over=get_cutover_value(plans[0].get("current_cutover", None)),
    )


@pytest.mark.tier1
@pytest.mark.warm
@pytest.mark.negative
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-rhel8-warm-149-no-cbt",
                        },
                    ],
                    "warm_migration": True,
                    "check_vms_signals": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-149 rhel_vm_disabled_cbt"],
)
def test_mtv_warm_p1_negative(
    skip_if_no_vmware,
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
        condition_message=MTV.ConditionMessage.PLAN_FAILED,
        condition_type=MTV.ConditionType.FAILED,
    )


@pytest.mark.tier2
@pytest.mark.warm
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": "mtv-rhel8-warm-203", "source_vm_power": "on"},
                    ],
                    "warm_migration": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-203 warm:false"],
)
def test_mtv_warm_p2(
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
    )


@pytest.mark.warmscale
def test_mtv_migration_scale_warm(
    plans_scale,
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
        plans=plans_scale,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
    )


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": "mtv-rhel8-warm-datacheck", "source_vm_power": "on"},
                    ],
                    "warm_migration": True,
                    "pre_copies_before_cut_over": 2,
                }
            ],
        ),
    ],
    indirect=True,
    ids=["MTV-212 cut-off between snapshots"],
)
@pytest.mark.warm
@pytest.mark.warm_with_data_check
def test_warm_with_data_check(
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    precopy_interval_forkliftcontroller,
    network_migration_map_pod_only,
    storage_migration_map,
    skip_if_no_vmware,
):
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map_pod_only,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
    )


@pytest.mark.tier1
@pytest.mark.warm
@pytest.mark.source_admin
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": "mtv-rhel8-warm-334", "source_vm_power": "on"},
                    ],
                    "warm_migration": True,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-334"],
)
def test_warm_source_provider_admin_user(
    skip_if_no_vmware,
    plans,
    source_provider_data,
    source_provider_admin_user,
    destination_provider,
    precopy_interval_forkliftcontroller,
    network_migration_map_source_admin,
    storage_migration_map_source_admin,
):
    migrate_vms(
        source_provider=source_provider_admin_user,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map_source_admin,
        storage_migration_map=storage_migration_map_source_admin,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
    )


@pytest.mark.tier1
@pytest.mark.warm
@pytest.mark.negative
@pytest.mark.source_non_admin
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-rhel8-warm-325",
                        },
                    ],
                    "warm_migration": True,
                    "check_vms_signals": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-325"],
)
def test_warm_negative_source_provider_non_admin(
    skip_if_no_vmware,
    plans,
    source_provider_data,
    source_provider_non_admin_user,
    destination_provider,
    precopy_interval_forkliftcontroller,
    network_migration_map_source_non_admin,
    storage_migration_map_source_non_admin,
):
    migrate_vms(
        source_provider=source_provider_non_admin_user,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map_source_non_admin,
        storage_migration_map=storage_migration_map_source_non_admin,
        source_provider_data=source_provider_data,
        cut_over=get_cutover_value(),
        condition_message=MTV.ConditionMessage.PLAN_FAILED,
        condition_type=MTV.ConditionType.FAILED,
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
@pytest.mark.skipif(not config.get("remote_ocp_cluster", False), reason="remote_ocp_cluster=false")
def test_warm_remote_ocp(
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
    )
