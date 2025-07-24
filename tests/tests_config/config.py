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

for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore # noqa: F821
