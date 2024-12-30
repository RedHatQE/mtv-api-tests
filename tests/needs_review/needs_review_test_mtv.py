import subprocess

import pytest as pytest
from ocp_resources.resource import Resource
from pytest_testconfig import py_config
from ocp_resources.storage_class import StorageClass
from ocp_resources.plan import Plan
from report import create_migration_scale_report
from utilities.mtv_migration import migrate_vms

STORAGE_SUFFIX = ""
if py_config["matrix_test"]:
    SC = py_config["storage_class"]
    if "ceph-rbd" in SC:
        STORAGE_SUFFIX = "-ceph-rbd"
    elif "nfs" in SC:
        STORAGE_SUFFIX = "-nfs"


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": f"mtv-rhel8-sanity{STORAGE_SUFFIX}", "guest_agent": True},
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


@pytest.mark.parametrize(
    "plans",
    [
        # MTV-76
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "v2v-migration-rhel8-12gb",
                        },
                        {"name": "v2v-migration-win2019", "source_vm_power": "on"},
                    ],
                    "warm_migration": False,
                }
            ],
        ),
        # MTV-78
        # MTV-197
        # MTV-81
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {"name": "mtv-rhel8-thick-eager", "source_vm_power": "on"},
                        {
                            "name": "mtv-rhel8-thick-lazy",
                        },
                        # {
                        #     "name": "mtv-win10-thin",
                        #     "source_vm_power": "on"
                        # },
                    ],
                    "warm_migration": False,
                }
            ],
        ),
        # MTV-77
        # MTV-82
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "v2v-migration-rhel8-2disks2nics",
                        },
                        {
                            "name": "v2v-migration-rhel8-char63longssssssssssssssssss2diskssssssssss",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
            # TODO fix Polarion ID
        ),
    ],
    indirect=True,
    ids=["MTV-76", "MTV-78|MTV-197|MTV-81", "MTV-77|MTV-82"],
)
@pytest.mark.tier1
def test_mtv_migration(
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


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-rh7-1disk-1nic",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
            # MTV-262
        )
    ],
    indirect=True,
    ids=["MTV-262"],
)
@pytest.mark.tier2
def test_mtv_migration_with_hooks(
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    network_migration_map,
    storage_migration_map,
    prehook,
    posthook,
):
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        pre_hook_name=prehook.name,
        pre_hook_namespace=prehook.namespace,
        after_hook_name=posthook.name,
        after_hook_namespace=posthook.namespace,
        target_namespace=target_namespace,
    )


@pytest.mark.tier2
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-rhel8-nonics",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-102"],
)
def test_mtv_migration_no_nics(
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


@pytest.mark.tier2
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-migration-migratable",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-219"],
)
def test_mtv_migration_migratable(
    skip_if_no_rhv,  # Reason: Related to 'Host->Migration mode' setting that can be set on RHV
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


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-migration-non-utc-with-usb",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
            # MTV-99
            # MTV-320
        )
    ],
    indirect=True,
    ids=["MTV-99|MTV-320"],
)
@pytest.mark.tier2
def test_mtv_migration_non_utc(
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
                        {"name": f"mtv-rhel8-79{STORAGE_SUFFIX}"},
                        {
                            "name": f"mtv-win2019-79{STORAGE_SUFFIX}",
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


@pytest.mark.scale
def test_mtv_migration_scale(
    target_namespace,
    plans_scale,
    source_provider,
    source_provider_data,
    destination_provider,
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
        target_namespace=target_namespace,
    )


@pytest.mark.interop
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "v2v-migration-rhel8-interop-1",
                        },
                        {
                            "name": "v2v-migration-rhel8-interop-2",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
            # TODO fix Polarion ID
        )
    ],
    indirect=True,
)
def test_mtv_migration_interop(
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


@pytest.mark.set
def test_mtv_migration_set(
    target_namespace,
    plans_set,
    source_provider,
    source_provider_data,
    destination_provider,
    network_migration_map,
    storage_migration_map,
):
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans_set,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
    )


@pytest.mark.reportonly
def test_report_only(
    admin_client,
):
    plan_resource = next(Plan.get(dyn_client=admin_client, name=py_config.get("planname"), namespace="openshift-mtv"))
    create_migration_scale_report(plan_resource=plan_resource)


@pytest.mark.create_all_providers
def test_create_all_providers(source_providers):
    pass


@pytest.mark.vmware_network_selection
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "v2v-migration-rhel8-for-migration-network-1-disk",
                        },
                        {
                            "name": "v2v-migration-rhel8-for-migration-network-2-disk",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
            # TODO fix Polarion ID
        )
    ],
    indirect=True,
    ids=["MTV-311"],
)
def test_mtv_migration_vmware_network_selection(
    skip_if_no_vmware,
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    network_migration_map,
    storage_migration_map,
    source_provider_host,
):
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        source_provider_host=source_provider_host,
        target_namespace=target_namespace,
    )


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-rhel8-333",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-333"],
)
@pytest.mark.tier1
@pytest.mark.source_admin
def test_cold_source_provider_admin_user(
    skip_if_no_vmware,
    target_namespace,
    plans,
    source_provider_data,
    source_provider_admin_user,
    destination_provider,
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
        target_namespace=target_namespace,
    )


@pytest.mark.cert
# @pytest.mark.tier1
def test_external_ingress_cert_mtv_348(restore_ingress_certificate):
    assert subprocess.run(["/bin/sh", "./utilities/publish.sh"]).returncode == 0, "external certification check"


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-rhel8-353",
                        },
                    ],
                    "warm_migration": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-353"],
)
@pytest.mark.tier1
@pytest.mark.customer_case
@pytest.mark.skipif(
    py_config.get("storage_class") != StorageClass.Types.CEPH_RBD, reason="Skip testing. Storage Class is not CEPH_RBD"
)
def test_customer_case_bz_2064936(
    skip_if_no_vmware,
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    network_migration_map,
    storage_migration_map_default_settings,
):
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map_default_settings,
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
                        {
                            "name": "mtv-rhel8_92",
                        },
                    ],
                    "warm_migration": False,
                    "check_vms_signals": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-92"],
)
@pytest.mark.tier1
@pytest.mark.negative
def test_negative_non_compatible_source_vm_name(
    skip_if_no_rhv,  # In order to save time, test does not depend on provider.
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
        expected_plan_ready=False,
        condition_type=Resource.Condition.Status.TARGET_NAME_NOT_VALID,
        target_namespace=target_namespace,
    )


@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-rhel8-108",
                        },
                    ],
                    "warm_migration": False,
                    "check_vms_signals": False,
                    "expected_plan_ready": True,
                    "condition_category": None,
                    "condition_type": Resource.Status.SUCCEEDED,
                }
            ],
        ),
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "mtv-rhel8-108",
                        },
                    ],
                    "warm_migration": False,
                    "check_vms_signals": False,
                    "expected_plan_ready": False,
                    "condition_type": Resource.Status.VM_ALREADY_EXISTS,
                }
            ],
        ),
    ],
    indirect=True,
    ids=["MTV-108 plan1", "MTV-108 plan2"],
)
@pytest.mark.tier1
@pytest.mark.negative
def test_negative_same_source_vm(
    skip_if_no_rhv,  # In order to save time, test does not depend on provider.
    target_namespace,
    plans,
    source_provider,
    source_provider_data,
    destination_provider,
    network_migration_map,
    storage_migration_map,
):
    """
    In order to test MTV-108 two plans will be executed one after the other with the same source VM
    """
    migrate_vms(
        source_provider=source_provider,
        destination_provider=destination_provider,
        plans=plans,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        expected_plan_ready=plans[0]["expected_plan_ready"],
        condition_status=plans[0]["condition_category"],
        condition_type=plans[0]["condition_type"],
        target_namespace=target_namespace,
    )


@pytest.mark.ocp
@pytest.mark.parametrize(
    "plans",
    [
        # MTV-xx
        pytest.param(
            [
                {
                    "virtual_machines": [{"name": f"cnv-rhel9-2disks2nics{STORAGE_SUFFIX}", "source_vm_power": "on"}],
                    "warm_migration": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-ocp"],
)
@pytest.mark.skipif(not py_config.get("remote_ocp_cluster", False), reason="remote_ocp_cluster=false")
def test_cold_ocp_to_ocp(
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


@pytest.mark.ova
@pytest.mark.parametrize(
    "plans",
    [
        pytest.param(
            [
                {
                    "virtual_machines": [
                        {
                            "name": "ubuntu_14_10_amd64",
                        },
                    ],
                    "warm_migration": False,
                    "check_vms_signals": False,
                }
            ],
        )
    ],
    indirect=True,
    ids=["MTV-ova"],
)
def test_cold_ova(
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
