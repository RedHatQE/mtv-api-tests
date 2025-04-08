global config

source_providers_list = [
    {
        "type": "ovirt",
        "version": "4.4.9",
        "fqdn": "rhev-red-02.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-red-02.rdu2.scalelab.redhat.com/ovirt-engine/api",
        "username": "admin@internal",
        "password": "qum5net",
        "cluster_name": "MTV-CNV",
        "default": "True",
    }
]
storage_class = "ocs-storagecluster-ceph-rbd"
source_provider_type = "ovirt"
source_provider_version = "4.4.9"
target_namespace = "openshift-mtv"
mtv_namespace = "openshift-mtv"
vm_name_search_pattern = "auto-rhv-red-migwarm-50gb-70usage"
number_of_vms = 20
check_vms_signals = False
create_scale_report = True
plan_wait_timeout = 3600
snapshots_interval = 5
mins_before_cutover = 20


for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore # noqa: F821
