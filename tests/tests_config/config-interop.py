global config

source_providers_list = [
    {
        "type": "vsphere",
        "version": "7.0",
        "fqdn": "rhev-node-13.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-node-13.rdu2.scalelab.redhat.com/sdk",
        "username": "administrator@vsphere.local",
        "password": "LetMein8!",
        "cluster_name": "MTV",
        "default": "True",
    },
    {
        "type": "ovirt",
        "version": "4.4.9",
        "fqdn": "rhev-red-02.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-red-02.rdu2.scalelab.redhat.com/ovirt-engine/api",
        "username": "admin@internal",
        "password": "qum5net",
        "cluster_name": "MTV-CNV",
        "default": "True",
    },
]
storage_class = "nfs"
source_provider_type = "ovirt"
source_provider_version = "4.4.9"
warm_migration = False
check_vms_signals = False
target_namespace = "mtv-api-tests"
delete_target_namespace = True


for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore   # noqa: F821
