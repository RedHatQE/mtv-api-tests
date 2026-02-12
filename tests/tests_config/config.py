global config

insecure_verify_skip: str = "true"  # SSL verification for OCP API connections
source_provider_insecure_skip_verify: str = "true"  # SSL verification for source provider (VMware, RHV, etc.)
number_of_vms: int = 1
check_vms_signals: bool = True
target_namespace_prefix: str = "auto"
mtv_namespace: str = "openshift-mtv"
vm_name_search_pattern: str = ""
remote_ocp_cluster: str = ""
snapshots_interval: int = 2
mins_before_cutover: int = 5
plan_wait_timeout: int = 3600

tests_params: dict = {
    "test_sanity_warm_mtv_migration": {
        "virtual_machines": [
            {
                "name": "mtv-tests-rhel8",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": True,
    },
    "test_mtv_migration_warm_2disks2nics": {
        "virtual_machines": [
            {
                "name": "mtv-rhel8-warm-2disks2nics",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": True,
    },
    "test_warm_remote_ocp": {
        "virtual_machines": [
            {
                "name": "mtv-tests-rhel8",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": True,
    },
    "test_sanity_cold_mtv_migration": {
        "virtual_machines": [
            {"name": "mtv-tests-rhel8", "guest_agent": True},
        ],
        "warm_migration": False,
    },
    "test_cold_remote_ocp": {
        "virtual_machines": [
            {"name": "mtv-tests-rhel8"},
            {
                "name": "mtv-win2019-79",
            },
        ],
        "warm_migration": False,
    },
    "test_copyoffload_thin_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_thick_lazy_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thick-lazy",
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_multi_disk_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "add_disks": [
                    {"size_gb": 30, "disk_mode": "persistent", "provision_type": "thick-lazy"},
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_multi_disk_different_path_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "add_disks": [
                    {
                        "size_gb": 30,
                        "disk_mode": "persistent",
                        "provision_type": "thick-lazy",
                        "datastore_path": "shared_disks",
                    },
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_rdm_virtual_disk_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "add_disks": [
                    {"rdm_type": "virtual"},  # LUN UUID from copyoffload.rdm_lun_uuid
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_thin_snapshots_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "snapshots": 2,  # Number of snapshots to create on the source VM before migration
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_multi_datastore_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {
                        "size_gb": 30,
                        "disk_mode": "persistent",
                        "provision_type": "thin",
                        "datastore_id": "secondary_datastore_id",
                    },
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_independent_persistent_disk_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {
                        "size_gb": 30,
                        "disk_mode": "independent_persistent",
                        "provision_type": "thin",
                    },
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_independent_nonpersistent_disk_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {
                        "size_gb": 30,
                        "disk_mode": "independent_nonpersistent",
                        "provision_type": "thin",
                    },
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_10_mixed_disks_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thick-lazy"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thick-lazy"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thick-lazy"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thick-lazy"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thin"},
                    {"size_gb": 10, "disk_mode": "persistent", "provision_type": "thick-lazy"},
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_large_vm_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {
                        "size_gb": 1024,
                        "disk_mode": "persistent",
                        "provision_type": "thin",
                    },
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_warm_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "on",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
            },
        ],
        "warm_migration": True,
        "copyoffload": True,
    },
    "test_copyoffload_2tb_vm_snapshots_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {
                        "size_gb": 2048,
                        "disk_mode": "persistent",
                        "provision_type": "thin",
                    },
                ],
                "snapshots": 2,  # Number of snapshots to create on the source VM before migration
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_pre_hook_succeed_post_hook_fail": {
        "virtual_machines": [
            {
                "name": "mtv-tests-rhel8",
                "source_vm_power": "off",
            },
        ],
        "warm_migration": False,
        "pre_hook": {
            "expected_result": "succeed",
        },
        "post_hook": {
            "expected_result": "fail",
        },
        "expected_migration_result": "fail",
    },
    "test_copyoffload_mixed_datastore_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
                "add_disks": [
                    {
                        "size_gb": 30,
                        "provision_type": "thin",
                        "datastore_id": "non_xcopy_datastore_id",
                    },
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_simultaneous_copyoffload_migrations": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
            },
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_nonconforming_name_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "clone_name": "XCopy_Test_VM_CAPS",  # Non-conforming name for cloned VM
                "preserve_name_format": True,  # Don't sanitize the name (keep capitals and underscores)
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thin",
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_copyoffload_fallback_large_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "target_datastore_id": "non_xcopy_datastore_id",
                "disk_type": "thin",
                "add_disks": [
                    {
                        "size_gb": 100,
                        "provision_type": "thin",
                        "datastore_id": "non_xcopy_datastore_id",
                    },
                ],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
    "test_target_scheduling_all_features": {
        "virtual_machines": [
            {
                "name": "mtv-tests-rhel8",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": False,
        # MTV 2.10.0 target scheduling features
        "target_node_selector": {
            "mtv-test-node": None,  # None = auto-generate with session_uuid
        },
        "target_labels": {
            "mtv-test-label": None,  # None = auto-generate with session_uuid
            "custom-label": "custom-value",  # Static value
        },
        "target_affinity": {
            "podAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": {"app": "test"}},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                        "weight": 50,
                    }
                ]
            }
        },
    },
    "test_custom_nad_vm_namespace": {
        "virtual_machines": [
            {
                "name": "mtv-tests-rhel8",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": False,
        "vm_target_namespace": "custom-vm-namespace",
        "multus_namespace": "default",
    },
    "test_copyoffload_scale_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thick-lazy",
                "add_disks": [{"size_gb": 30, "provision_type": "thick-lazy", "disk_mode": "persistent"}],
            },
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thick-lazy",
                "add_disks": [{"size_gb": 30, "provision_type": "thick-lazy", "disk_mode": "persistent"}],
            },
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thick-lazy",
                "add_disks": [{"size_gb": 30, "provision_type": "thick-lazy", "disk_mode": "persistent"}],
            },
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thick-lazy",
                "add_disks": [{"size_gb": 30, "provision_type": "thick-lazy", "disk_mode": "persistent"}],
            },
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thick-lazy",
                "add_disks": [{"size_gb": 30, "provision_type": "thick-lazy", "disk_mode": "persistent"}],
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
        "guest_agent_timeout": 600,
    },
    "test_concurrent_xcopy_vddk_migration": {
        "virtual_machines": [
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thick-lazy",
            },
            {
                "name": "xcopy-template-test",
                "source_vm_power": "off",
                "guest_agent": True,
                "clone": True,
                "disk_type": "thick-lazy",
            },
        ],
        "warm_migration": False,
        "copyoffload": True,
    },
}

for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file", "__annotations__"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore # noqa: F821
