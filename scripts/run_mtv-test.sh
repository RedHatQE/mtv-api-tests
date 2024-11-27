#!/usr/bin/bash
set -ex

RUN_TEST="sh run_mtv-test.sh"

usage() {
  cat <<EOF
Usage:
${RUN_TEST} [options]

Options:
  -s The test scope
  -t The test type
  -k The kubeconfig root dir
  -l The local cluster name
  -r The remote cluster name
  -d The temporary direcotory
  -h Print help

Examples:
1. Run release local testing
${RUN_TEST} -s "release" -t "local" -k "/tmp/kube_config_root_dir" -l "qemtv-01" -d "/tmp/mtv-test"
2. Run release remote testing
${RUN_TEST} -s "release" -t "remote" -k "/tmp/kube_config_root_dir" -l "qemtv-01" -r "qemtv-02" -d "/tmp/mtv-test"
3. Run stage local testing
${RUN_TEST} -s "stage" -t "local" -k "/tmp/kube_config_root_dir" -l "qemtv-01" -d "/tmp/mtv-test"
4. Run stage remote testing
${RUN_TEST} -s "stage" -t "remote" -k "/tmp/kube_config_root_dir" -l "qemtv-01" -r "qemtv-02" -d "/tmp/mtv-test"
5. Run release non-gate local testing
${RUN_TEST} -s "non-gate" -t "local" -k "/tmp/kube_config_root_dir" -l "qemtv-01" -d "/tmp/mtv-test"
6. Run release non-gate remote testing
${RUN_TEST} -s "non-gate" -t "remote" -k "/tmp/kube_config_root_dir" -l "qemtv-01" -r "qemtv-02" -d "/tmp/mtv-test"
EOF
}

test_scope=${test_scope:-"release"}
test_type=${test_type:-"local"}
kube_config_root_dir=${kube_config_root_dir:-""}
local_cluster=${local_cluster:-""}
remote_cluster=${remote_cluster:-""}
temporary_dir=${temporary_dir:-""}

while getopts s:t:k:l:r:d:h: options; do
  case $options in
  s) test_scope=$OPTARG ;;
  t) test_type=$OPTARG ;;
  k) kube_config_root_dir=$OPTARG ;;
  l) local_cluster=$OPTARG ;;
  r) remote_cluster=$OPTARG ;;
  d) temporary_dir=$OPTARG ;;
  h)
    usage
    exit 0
    ;;
  ?)
    usage
    exit 1
    ;;
  esac
done

echo 'Mount the kubecofig'
if [[ ! -d $kube_config_root_dir ]]; then
  mkdir -p $kube_config_root_dir
fi

mountpoint $kube_config_root_dir
if [[ $? != 0 ]]; then
  echo "$kube_config_root_dir is not mounted with rhos_psi_cluster_dirs. Please mount it first."
  sudo -S mount -t nfs 10.9.96.21:/rhos_psi_cluster_dirs $kube_config_root_dir
fi

local_kube_config=$kube_config_root_dir/$local_cluster/auth/kubeconfig
export KUBECONFIG=$local_kube_config

echo 'Deploy pip enviroment'
sudo yum install python3 python3-devel libxml2-devel libcurl openssl openssl-devel gcc -y
sudo pip3 install uv

if [[ ! -d $temporary_dir ]]; then
  mkdir -p $temporary_dir
fi

if [[ ! -d $temporary_dir/mtv-api-tests ]]; then
  git clone https://gitlab.cee.redhat.com/mtv-qe/mtv-api-tests.git $temporary_dir/mtv-api-tests
fi

cd $temporary_dir/mtv-api-tests && uv install --python 3 --skip-lock
pip install --upgrade pyvmomi

if [[ ! -d $temporary_dir/rp-uploader ]]; then
  mkdir -p $temporary_dir/rp-uploader
fi

echo 'Run Tests'

# Release-gate-remote
if [[ $test_scope == "release" && $test_type == "remote" && -n "$local_cluster" && -n "$remote_cluster" ]]; then
  uv run pytest -m remote \
    --tc=matrix_test:true \
    --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
    --tc=source_provider_type:"vsphere" \
    --tc=source_provider_version:"7.0.3" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-vsphere" \
    --tc=remote_ocp_cluster:"$remote_cluster" >$temporary_dir/rp-uploader/vsphere_remote_ceph-rbd.xml

  uv run pytest -m remote \
    --tc=matrix_test:true \
    --tc=storage_class:"standard-csi" \
    --tc=source_provider_type:"vsphere" \
    --tc=source_provider_version:"6.5" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-vsphere" \
    --tc=remote_ocp_cluster:"$remote_cluster" >$temporary_dir/rp-uploader/vsphere_remote_standard-csi.xml
fi

# Release-gate-local
if [[ $test_scope == "release" && $test_type == "local" && -n "$local_cluster" ]]; then
  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
    --tc=source_provider_type:"vsphere" \
    --tc=source_provider_version:"7.0.3" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-vsphere" >$temporary_dir/rp-uploader/vsphere_ceph-rbd.xml

  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"nfs-csi" \
    --tc=source_provider_type:"vsphere" \
    --tc=source_provider_version:"8.0.1" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-vsphere" >$temporary_dir/rp-uploader/vsphere_nfs-csi.xml
fi

# Stage-gate-remote
if [[ $test_scope == "stage" && $test_type == "remote" && -n "$local_cluster" && -n "$remote_cluster" ]]; then
  uv run pytest -m remote \
    --tc=matrix_test:true \
    --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
    --tc=source_provider_type:"vsphere" \
    --tc=source_provider_version:"7.0.3" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-vsphere" \
    --tc=remote_ocp_cluster:"$remote_cluster" >$temporary_dir/rp-uploader/vsphere_remote_ceph-rbd.xml
fi

# Stage-gate-local
if [[ $test_scope == "stage" && $test_type == "local" && -n "$local_cluster" ]]; then
  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
    --tc=source_provider_type:"vsphere" \
    --tc=source_provider_version:"7.0.3" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-vsphere" >$temporary_dir/rp-uploader/vsphere_ceph-rbd.xml
fi

# Release-non-gate-remote
if [[ $test_scope == "non-gate" && $test_type == "remote" && -n "$local_cluster" && -n "$remote_cluster" ]]; then
  uv run pytest -m remote \
    --tc=matrix_test:true \
    --tc=storage_class:"standard-csi" \
    --tc=source_provider_type:"ovirt" \
    --tc=source_provider_version:"4.4.9" \
    --tc=target_namespace:"mtv-api-tests-ovirt" \
    --tc=remote_ocp_cluster:"$remote_cluster" >$temporary_dir/rp-uploader/ovirt_remote_standard-csi.xml
fi

# Release-non-gate
if [[ $test_scope == "non-gate" && $test_type == "local" && -n "$local_cluster" ]]; then
  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"standard-csi" \
    --tc=source_provider_type:"ovirt" \
    --tc=source_provider_version:"4.4.9" \
    --tc=target_namespace:"mtv-api-tests-ovirt" >$temporary_dir/rp-uploader/ovirt_standard-csi.xml

  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"standard-csi" \
    --tc=source_provider_type:"openstack" \
    --tc=source_provider_version:"psi" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-openstack" >$temporary_dir/rp-uploader/openstack_standard-csi.xml
fi
