global config
source_providers_list = [
    {
        "type": "vsphere",
        "version": "6.5",
        "fqdn": "rhev-node-05.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-node-05.rdu2.scalelab.redhat.com/sdk",
        "vddk_init_image": "quay.io/qiyuan1/test7",
        "username": "mtv@vsphere.local",
        "password": "Heslo123!",
        "cluster_name": "MTV",
        "default": "True",
    }
]
storage_class = "ocs-storagecluster-ceph-rbd"
source_provider_type = "vsphere"
source_provider_version = "6.5"
target_namespace = "openshift-mtv"
mtv_namespace = "openshift-mtv"
vm_name_search_pattern = "automation-dc65-iscsi-scalevm-50gb-70usage"
number_of_vms = 20
warm_migration = False
check_vms_signals = False
create_scale_report = True
plan_wait_timeout = 3600


for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore # noqa: F821
