global config

insecure_verify_skip: str = "true"
number_of_vms: int = 1
check_vms_signals: bool = True
target_namespace_prefix: str = "mtv-api-tests"
mtv_namespace: str = "openshift-mtv"
vm_name_search_pattern: str = ""
remote_ocp_cluster: str = ""
snapshots_interval: int = 2
mins_before_cutover: int = 5
plan_wait_timeout: int = 3600
matrix_test: bool = True
release_test: bool = False
mount_root: str = ""

tests_params: dict = {
    "test_sanity_warm_mtv_migration": {
        "virtual_machines": [
            {
                "name": "mtv-rhel8-warm-sanity",
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
                "name": "mtv-rhel8-warm-394",
                "source_vm_power": "on",
                "guest_agent": True,
            },
        ],
        "warm_migration": True,
    },
    "test_sanity_cold_mtv_migration": {
        "virtual_machines": [
            {"name": "mtv-rhel8-sanity", "guest_agent": True},
        ],
        "warm_migration": False,
    },
    "test_cold_remote_ocp": {
        "virtual_machines": [
            {"name": "mtv-rhel8-79"},
            {
                "name": "mtv-win2019-79",
            },
        ],
        "warm_migration": False,
    },
}

for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file", "__annotations__"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore # noqa: F821
