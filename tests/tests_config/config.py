global config

source_providers_list = [
    {
        "type": "vsphere",
        "version": "6.5",
        "fqdn": "rhev-node-05.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-node-05.rdu2.scalelab.redhat.com/sdk",
        "username": "mtv@vsphere.local",
        "password": "Heslo123!",
        "admin_username": "administrator@vsphere.local",
        "admin_password": "Heslo123!",
        "non_admin_username": "nonadmin@vsphere.local",
        "non_admin_password": "Heslo123!",
        "cluster_name": "",
        "default": "True",
        "vm_folder": "mtv-func-qe-api-automation",
        "guest_vm_linux_user": "root",
        "guest_vm_linux_password": "100yard-",
        "guest_vm_win_user": "Administrator",
        "guest_vm_win_password": "Heslo123!",
        "vddk_init_image": "quay.io/qiyuan1/test7",
        "host_list": [
            {
                "migration_host_id": "host-732",
                "migration_host_ip": "172.16.11.6",
                "user": "root",
                "password": "Mypassw0rd",
                "default": "True",
            }
        ],
        "networks": [{"name": "VM Network"}, {"name": "Mgmt Network"}],
        "storages": [{"name": "rhv-v2v-performance-testing"}],
    },
    {
        "type": "vsphere",
        "version": "7.0.3",
        "fqdn": "10.6.46.213",
        "api_url": "https://10.6.46.213/sdk",
        "username": "administrator@vsphere.local",
        "password": "VCENTER@redhat2023",
        "admin_username": "administrator@vsphere.local",
        "admin_password": "VCENTER@redhat2023",
        "cluster_name": "",
        "default": "True",
        "guest_vm_linux_user": "root",
        "guest_vm_linux_password": "redhat",
        "guest_vm_win_user": "Administrator",
        "guest_vm_win_password": "123qweP",
        "vddk_init_image": "quay.io/libvirt_v2v_cnv/vddk:7.0.3",
        "host_list": [
            {
                "migration_host_id": "host-10",
                "migration_host_ip": "10.6.46.28",
                "user": "root",
                "password": "123qweP",
                "default": "True",
            }
        ],
        "networks": [{"name": "VM Network"}, {"name": "Mgmt Network"}],
        "storages": [{"name": "nfs-us-mtv-v7"}],
    },
    {
        "type": "vsphere",
        "version": "8.0.1",
        "fqdn": "10.6.46.250",
        "api_url": "https://10.6.46.250/sdk",
        "username": "administrator@vsphere.local",
        "password": "VCENTER@redhat2023",
        "admin_username": "administrator@vsphere.local",
        "admin_password": "VCENTER@redhat2023",
        "cluster_name": "",
        "default": "True",
        "guest_vm_linux_user": "root",
        "guest_vm_linux_password": "redhat",
        "guest_vm_win_user": "Administrator",
        "guest_vm_win_password": "123qweP",
        "vddk_init_image": "quay.io/libvirt_v2v_cnv/vddk:8.0.1",
        "host_list": [
            {
                "migration_host_id": "host-8",
                "migration_host_ip": "10.6.46.30",
                "user": "root",
                "password": "123qweP",
                "default": "True",
            }
        ],
        "networks": [{"name": "VM Network"}, {"name": "Mgmt Network"}],
        "storages": [{"name": "nfs-us-mtv-v8"}],
    },
    {
        "type": "vsphere",
        "version": "6.7",
        "fqdn": "rhev-node-15.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-node-15.rdu2.scalelab.redhat.com/sdk",
        "username": "mtv@vsphere.local",
        "password": "LetMein8!",
        "admin_username": "administrator@vsphere.local",
        "admin_password": "LetMein8!",
        "cluster_name": "",
        "default": "True",
        "vddk_init_image": "quay.io/qiyuan1/test7",
    },
    {
        "type": "vsphere",
        "version": "7.0",
        "fqdn": "rhev-node-13.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-node-13.rdu2.scalelab.redhat.com/sdk",
        "username": "administrator@vsphere.local",
        "password": "Power123!",
        "admin_username": "administrator@vsphere.local",
        "admin_password": "Power123!",
        "cluster_name": "",
        "default": "True",
        "vddk_init_image": "quay.io/qiyuan1/test7",
        "networks": [{"name": "VM Network"}, {"name": "Mgmt Network"}],
        "storages": [{"name": "f01-h03-ESX-local-OS"}],
        "vm_folder": "mtv-ci-test",
    },
    {
        "type": "ovirt",
        "version": "4.4.9",
        "fqdn": "rhev-red-02.rdu2.scalelab.redhat.com",
        "api_url": "https://rhev-red-02.rdu2.scalelab.redhat.com/ovirt-engine/api",
        "username": "admin@internal",
        "password": "qum5net",
        "cluster_name": "",
        "default": "True",
        "networks": [{"id": "4b389d67-36ed-4984-8854-82d0d72be966"}, {"id": "adbe17e1-9424-4454-8d59-a74b8c5486f7"}],
        "storages": [{"name": "nfs_mtv_0"}],
    },
    {
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
        "default": "True",
        "guest_vm_linux_user": "root",
        "guest_vm_linux_password": "redhat123456",
        "networks": [{"id": "316eeb47-1498-46b4-b39e-00ddf73bd2a5"}],
        "storages": [{"name": "tripleo"}],
    },
    {
        "type": "openshift",
        "version": "localhost",
        "default": "True",
        "networks": [{"type": "pod"}, {"type": "multus", "name": "mtv-api-tests-ocp/mybridge"}],
        "storages": [{"name": "hostpath-csi-basic"}, {"name": "ocs-storagecluster-ceph-rbd"}],
    },
    {
        "type": "ova",
        "version": "nfs",
        "fqdn": "",
        "api_url": "f02-h06-000-r640.rdu2.scalelab.redhat.com:/home/nfsshare-test",
        "username": "ova",
        "password": "",
        "default": "True",
        "networks": [{"type": "pod"}, {"type": "multus", "name": "mtv-api-tests-ocp/mybridge"}],
        "storages": [{"name": "Dummy storage for source provider ova"}],
    },
]
hook_dict = {
    "prehook": {
        "name": "prehook-ansible",
        "payload": "LS0tCi0gbmFtZTogTWFpbgogIGhvc3RzOiBsb2NhbGhvc3QKICB0YXNrczoKICAtIG5hbWU6IExvYWQgUGxhbgogICAgaW5jbHVkZV92YXJzOgogICAgICBmaWxlOiBwbGFuLnltbAogICAgICBuYW1lOiBwbGFuCiAgLSBuYW1lOiBMb2FkIFdvcmtsb2FkCiAgICBpbmNsdWRlX3ZhcnM6CiAgICAgIGZpbGU6IHdvcmtsb2FkLnltbAogICAgICBuYW1lOiB3b3JrbG9hZAoK",
    },
    "posthook": {
        "name": "posthook-ansible",
        "payload": "LS0tCi0gbmFtZTogTWFpbgogIGhvc3RzOiBsb2NhbGhvc3QKICB0YXNrczoKICAtIG5hbWU6IExvYWQgUGxhbgogICAgaW5jbHVkZV92YXJzOgogICAgICBmaWxlOiBwbGFuLnltbAogICAgICBuYW1lOiBwbGFuCiAgLSBuYW1lOiBMb2FkIFdvcmtsb2FkCiAgICBpbmNsdWRlX3ZhcnM6CiAgICAgIGZpbGU6IHdvcmtsb2FkLnltbAogICAgICBuYW1lOiB3b3JrbG9hZAoK",
    },
}
storage_class = "nfs"
source_provider_type = "vsphere"
source_provider_version = "7.0.3"
insecure_verify_skip = "true"
number_of_vms = 1
warm_migration = False
check_vms_signals = True
target_namespace = "mtv-api-tests"
delete_target_namespace = False
mtv_namespace = "openshift-mtv"
list_of_vms_csv = ""
vm_name_search_pattern = ""
turn_on_vms = False
remote_ocp_cluster = False
snapshots_interval = 2
mins_before_cutover = 5
skip_migration = False
plan_wait_timeout = 3000
clean_target_resources = True
matrix_test = True
release_test = False
target_ocp_version = "4.17"
mount_root = ""

for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue

    if _dir in ["encoding", "py_file"]:
        continue

    config[_dir] = locals()[_dir]  # type: ignore # noqa: F821
