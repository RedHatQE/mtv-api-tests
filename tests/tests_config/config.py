from typing import Any

global config

source_providers_dict: dict[str, dict[str, Any]] = {
    "vsphere-6.5": {
        "type": "vsphere",
        "version": "6.5",
        "fqdn": "rhev-node-05.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-node-05.rdu2.scalelab.redhat.com/sdk",
        "username": "mtv@vsphere.local",
        "password": "Heslo123!",
        "guest_vm_linux_user": "root",
        "guest_vm_linux_password": "100yard-",
        "guest_vm_win_user": "Administrator",
        "guest_vm_win_password": "Heslo123!",
        "vddk_init_image": "quay.io/rh-openshift-mtv/vddk-init-image:6.5",
    },
    "vsphere-7.0.3": {
        "type": "vsphere",
        "version": "7.0.3",
        "fqdn": "10.6.46.170",
        "api_url": "https://10.6.46.170/sdk",
        "username": "administrator@vsphere.local",
        "password": "VCENTER@redhat2023",
        "guest_vm_linux_user": "root",
        "guest_vm_linux_password": "redhat",
        "guest_vm_win_user": "Administrator",
        "guest_vm_win_password": "123qweP",
        "vddk_init_image": "quay.io/rh-openshift-mtv/vddk-init-image:7.0.3",
    },
    "vsphere-8.0.1": {
        "type": "vsphere",
        "version": "8.0.1",
        "fqdn": "10.6.46.249",
        "api_url": "https://10.6.46.249/sdk",
        "username": "administrator@vsphere.local",
        "password": "VCENTER@redhat2023",
        "guest_vm_linux_user": "root",
        "guest_vm_linux_password": "redhat",
        "guest_vm_win_user": "Administrator",
        "guest_vm_win_password": "123qweP",
        "vddk_init_image": "quay.io/rh-openshift-mtv/vddk-init-image:8.0.1",
    },
    "ovirt-4.4.9": {
        "type": "ovirt",
        "version": "4.4.9",
        "fqdn": "rhev-red-02.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-red-02.rdu2.scalelab.redhat.com/ovirt-engine/api",
        "username": "admin@internal",
        "password": "qum5net",
    },
    "openstack-psi": {
        "type": "openstack",
        "version": "psi",
        "fqdn": "rhos-d.infra.prod.upshift.rdu2.redhat.com",
        "api_url": "https://rhos-d.infra.prod.upshift.rdu2.redhat.com:13000/v3",
        "username": "mtv-qe-user",
        "password": "Vm0pB_NAns_l2T5b1ZMHUKd6LqtPjQkH",
        "user_domain_name": "redhat.com",
        "region_name": "regionOne",
        "project_name": "mtv-qe-infra",
        "user_domain_id": "62cf1b5ec006489db99e2b0ebfb55f57",
        "project_domain_id": "62cf1b5ec006489db99e2b0ebfb55f57",
        "guest_vm_linux_user": "root",
        "guest_vm_linux_password": "redhat123456",
    },
    "openshift-localhost": {
        "type": "openshift",
        "version": "localhost",
        "networks": [{"type": "pod"}, {"type": "multus", "name": "mtv-api-tests-ocp/mybridge"}],
        "storages": [{"name": "hostpath-csi-basic"}, {"name": "ocs-storagecluster-ceph-rbd"}],
    },
    "ova-nfs": {
        "type": "ova",
        "version": "nfs",
        "fqdn": "",
        "api_url": "f02-h06-000-r640.rdu2.scalelab.redhat.com:/home/nfsshare-test/mtv-api-tests",
        "username": "ova",
        "password": "",
        # "default": "True",
    },
}

storage_class: str = "nfs"
source_provider_type: str = "vsphere"
source_provider_version: str = "7.0.3"
insecure_verify_skip: str = "true"
number_of_vms: int = 1
check_vms_signals: bool = True
target_namespace: str = "mtv-api-tests"
mtv_namespace: str = "openshift-mtv"
vm_name_search_pattern: str = ""
snapshots_interval: int = 2
mins_before_cutover: int = 5
plan_wait_timeout: int = 3600
matrix_test: bool = True
release_test: bool = False
target_ocp_version: str = "4.17"
mount_root: str = ""

for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore # noqa: F821
