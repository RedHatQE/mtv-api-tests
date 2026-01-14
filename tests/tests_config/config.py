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
}

for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file", "__annotations__"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore # noqa: F821
