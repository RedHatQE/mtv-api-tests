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
        "vddk_init_image": "quay.io/rh-openshift-mtv/vddk-init-image:6.5",
        "host_list": [
            {
                "migration_host_id": "host-732",
                "migration_host_ip": "172.16.11.6",
                "user": "root",
                "password": "Mypassw0rd",
                "default": "True",
            }
        ],
    },
    {
        "type": "vsphere",
        "version": "7.0.3",
        "fqdn": "10.6.46.170",
        "api_url": "https://10.6.46.170/sdk",
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
        "vddk_init_image": "quay.io/rh-openshift-mtv/vddk-init-image:7.0.3",
        "host_list": [
            {
                "migration_host_id": "host-10",
                "migration_host_ip": "10.6.46.28",
                "user": "root",
                "password": "123qweP",
                "default": "True",
            }
        ],
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
        "vddk_init_image": "quay.io/rh-openshift-mtv/vddk-init-image:8.0.1",
        "host_list": [
            {
                "migration_host_id": "host-8",
                "migration_host_ip": "10.6.46.29",
                "user": "root",
                "password": "123qweP",
                "default": "True",
            }
        ],
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
        "api_url": "f02-h06-000-r640.rdu2.scalelab.redhat.com:/home/nfsshare-test/mtv-api-tests",
        "username": "ova",
        "password": "",
        "default": "True",
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
check_vms_signals = True
target_namespace = "mtv-api-tests"
mtv_namespace = "openshift-mtv"
vm_name_search_pattern = ""
remote_ocp_cluster = ""
snapshots_interval = 2
mins_before_cutover = 5
plan_wait_timeout = 3600
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
