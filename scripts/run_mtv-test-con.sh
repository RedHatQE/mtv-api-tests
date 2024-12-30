#!/usr/bin/bash
set -ex

RUN_TEST="sh scripts/run_mtv-test-con.sh"

usage() {
  cat <<EOF
Usage:
${RUN_TEST} [options]

Options:
  -s The test scope (release, stage, non-gate, matrix)
  -p The provider type (vsphere7, vsphere8, vsphere6, rhv, osp)
  -t The test type (local, remote)
  -o The target storage type (ocs-storagecluster-ceph-rbd, standard-csi, nfs-csi)
  -k The kubeconfig root dir
  -l The local cluster name (qemtv-01)
  -r The remote cluster name (qemtv-02)
  -d The temporary direcotory
  -h Print help

Examples:
1. Run release local testing
   ${RUN_TEST} -s "release" -p "vsphere7" -t "local" -k "/tmp/auth_dir" -l "qemtv-01" -d "/tmp/mtv-test1"
   ${RUN_TEST} -s "release" -p "vsphere8" -t "local" -k "/tmp/auth_dir" -l "qemtv-01" -d "/tmp/mtv-test2"
2. Run release remote testing
   ${RUN_TEST} -s "release" -p "vsphere7" -t "remote" -k "/tmp/auth_dir" -l "qemtv-01" -r "qemtv-02" -d "/tmp/mtv-test3"
   ${RUN_TEST} -s "release" -p "vsphere6" -t "remote" -k "/tmp/auth_dir" -l "qemtv-01" -r "qemtv-02" -d "/tmp/mtv-test4"
3. Run stage local testing
   ${RUN_TEST} -s "stage" -t "local" -k "/tmp/auth_dir" -l "qemtv-01" -d "/tmp/mtv-test1"
4. Run stage remote testing
   ${RUN_TEST} -s "stage" -t "remote" -k "/tmp/auth_dir" -l "qemtv-01" -r "qemtv-02" -d "/tmp/mtv-test2"
5. Run release non-gate local testing
   ${RUN_TEST} -s "non-gate" -p "rhv" -t "local" -k "/tmp/auth_dir" -l "qemtv-01" -d "/tmp/mtv-test1"
   ${RUN_TEST} -s "non-gate" -p "osp" -t "local" -k "/tmp/auth_dir" -l "qemtv-01" -d "/tmp/mtv-test2"
6. Run release non-gate remote testing
   ${RUN_TEST} -s "non-gate" -p "rhv" -t "remote" -k "/tmp/auth_dir" -l "qemtv-01" -r "qemtv-02" -d "/tmp/mtv-test3"
7. Run matrix local testing
   ${RUN_TEST} -s "matrix" -p "vsphere7" -t "local" -o "ocs-storagecluster-ceph-rbd" -k "/tmp/auth_dir" -l "qemtv-01" -d "/tmp/mtv-test1"
8. Run matrix remote testing
   ${RUN_TEST} -s "matrix" -p "vsphere8" -t "remote" -o "standard-csi" -k "/tmp/auth_dir" -l "qemtv-01" -r "qemtv-01" -d "/tmp/mtv-test1"
9. Run matrix rhv remote testing
   ${RUN_TEST} -s "matrix" -p "rhv" -t "remote" -o "nfs-csi" -k "/tmp/auth_dir" -l "qemtv-01" -r "qemtv-02" -d "/tmp/mtv-test1"
10. Run matrix osp testing
   ${RUN_TEST} -s "matrix" -p "osp" -t "local" -o "standard-csi" -k "/tmp/auth_dir" -l "qemtv-01" -d "/tmp/mtv-test1"
EOF
}

test_scope=${test_scope:-""}
provider_type=${provider_type:-""}
test_type=${test_type:-""}
storage_type=${storage_type:-""}
auth_dir=${auth_dir:-""}
local_cluster=${local_cluster:-""}
remote_cluster=${remote_cluster:-""}
temp_dir=${temp_dir:-""}

while getopts s:p:t:o:k:l:r:d:h: options; do
  case $options in
  s) test_scope=$OPTARG ;;
  p) provider_type=$OPTARG ;;
  t) test_type=$OPTARG ;;
  o) storage_type=$OPTARG ;;
  k) auth_dir=$OPTARG ;;
  l) local_cluster=$OPTARG ;;
  r) remote_cluster=$OPTARG ;;
  d) temp_dir=$OPTARG ;;
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
if [[ ! -d $auth_dir ]]; then
  mkdir -p $auth_dir
fi

mountpoint $auth_dir
if [[ $? != 0 ]]; then
  echo "$auth_dir is not mounted with rhos_psi_cluster_dirs. Please mount it first."
  sudo -S mount -t nfs 10.9.96.21:/rhos_psi_cluster_dirs $auth_dir
fi

local_kube_config=$auth_dir/$local_cluster/auth/kubeconfig
export KUBECONFIG=$local_kube_config

echo 'Deploy pip enviroment'
sudo yum install python3 python3-devel libxml2-devel libcurl openssl openssl-devel gcc -y
sudo pip3 install uv

if [[ ! -d $temp_dir ]]; then
  mkdir -p $temp_dir
fi

if [[ ! -d $temp_dir/mtv-api-tests ]]; then
  git clone https://github.com/RedHatQE/mtv-api-tests.git
fi

cd $temp_dir/mtv-api-tests
uv install --python 3 --skip-lock
pip install --upgrade pyvmomi

echo "Prepare for testing"
case $provider_type in
vsphere7)
  p_type="vsphere"
  p_version="7.0.3"
  ;;
vsphere8)
  p_type="vsphere"
  p_version="8.0.1"
  ;;
vsphere6)
  p_type="vsphere"
  p_version="6.5"
  ;;
rhv)
  p_type="ovirt"
  p_version="4.4.9"
  ;;
osp)
  p_type="openstack"
  p_version="psi"
  ;;
*) echo "Don't support this provider" ;;
esac

log_dir="$temp_dir/rp-uploader"
if [[ ! -d $log_dir ]]; then
  mkdir -p $log_dir
fi

# Release gate remote
if [[ "$test_scope" == "release" && "$test_type" == "remote" && -n "$local_cluster" && -n "$remote_cluster" ]]; then
  case $provider_type in
  vsphere7) storage_type="ocs-storagecluster-ceph-rbd" ;;
  vsphere6) storage_type="standard-csi" ;;
  *) echo "For release gate remote testing, only include the scope for v7 and v6" ;;
  esac

  echo "Run release gate remote testing: $p_type-$p_version-$storage_type"
  uv run pytest -m remote \
    --tc=matrix_test:true \
    --tc=storage_class:"$storage_type" \
    --tc=source_provider_type:"$p_type" \
    --tc=source_provider_version:"$p_version" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-$provider_type" \
    --tc=remote_ocp_cluster:"$remote_cluster" >$log_dir/$p_type-$p_version-$storage_type-remote.xml
fi

# Release gate local
if [[ "$test_scope" == "release" && "$test_type" == "local" && -n "$local_cluster" ]]; then
  case $provider_type in
  vsphere7) storage_type="ocs-storagecluster-ceph-rbd" ;;
  vsphere8) storage_type="nfs-csi" ;;
  *) echo "For release gate remote testing, only include the scope for v7 and v8" ;;
  esac

  echo "Run release gate local testing: $p_type-$p_version-$storage_type"
  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"$storage_type" \
    --tc=source_provider_type:"$p_type" \
    --tc=source_provider_version:"$p_version" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-$provider_type" >$log_dir/$p_type-$p_version-$storage_type.xml
fi

# Stage gate remote
if [[ "$test_scope" == "stage" && "$test_type" == "remote" && -n "$local_cluster" && -n "$remote_cluster" ]]; then
  case $provider_type in
  vsphere7) storage_type="ocs-storagecluster-ceph-rbd" ;;
  *) echo "For state gate remote testing, only include the scope for v7" ;;
  esac

  echo "Run stage gate remote testing: $p_type-$p_version-$storage_type"
  uv run pytest -m remote \
    --tc=matrix_test:true \
    --tc=storage_class:"$storage_type" \
    --tc=source_provider_type:"$p_type" \
    --tc=source_provider_version:"$p_version" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-$provider_type" \
    --tc=remote_ocp_cluster:"$remote_cluster" >$log_dir/$p_type-$p_version-$storage_type-remote.xml
fi

# Stage gate local
if [[ "$test_scope" == "stage" && "$test_type" == "local" && -n "$local_cluster" ]]; then
  case $provider_type in
  vsphere7) storage_type="ocs-storagecluster-ceph-rbd" ;;
  *) echo "For state gate local testing, only include the scope for v7" ;;
  esac

  echo "Run stage gate local testing: $p_type-$p_version-$storage_type"
  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"$storage_typ" \
    --tc=source_provider_type:"$p_type" \
    --tc=source_provider_version:"$p_version" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-$provider_type" >$log_dir/$p_type-$p_version-$storage_type.xml
fi

# Release non-gate remote
if [[ "$test_scope" == "non-gate" && "$test_type" == "remote" && -n "$local_cluster" && -n "$remote_cluster" ]]; then
  case $provider_type in
  rhv) storage_type="standard-csi" ;;
  *) echo "For release non-gate remote testing, only include the scope for rhv" ;;
  esac

  echo "Run release non-gate remote testing: $p_type-$p_version-$storage_type"
  uv run pytest -m remote \
    --tc=matrix_test:true \
    --tc=storage_class:"$storage_type" \
    --tc=source_provider_type:"$p_type" \
    --tc=source_provider_version:"$p_version" \
    --tc=target_namespace:"mtv-api-tests-$provider_type" \
    --tc=remote_ocp_cluster:"$remote_cluster" >$log_dir/$p_type-$p_version-$storage_type-remote.xml
fi

# Release non-gate local
if [[ "$test_scope" == "non-gate" && "$test_type" == "local" && -n "$local_cluster" ]]; then
  case $provider_type in
  rhv) storage_type="standard-csi" ;;
  osp) storage_type="standard-csi" ;;
  *) echo "For release non-gate local testing, only include the scope for rhv and osp" ;;
  esac

  echo "Run release non-gate local testing: $p_type-$p_version-$storage_type"
  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"$storage_type" \
    --tc=source_provider_type:"$p_type" \
    --tc=source_provider_version:"$p_version" \
    --tc=target_namespace:"mtv-api-tests-$provider_type" >$log_dir/$p_type-$p_version-$storage_type.xml
fi

# Matrix remote
if [[ "$test_scope" == "matrix" && "$test_type" == "remote" && -n "$local_cluster" && -n "$remote_cluster" ]]; then
  echo "Run matrix remote testing: $p_type-$p_version-$storage_type"
  uv run pytest -m remote \
    --tc=matrix_test:true \
    --tc=storage_class:"$storage_type" \
    --tc=source_provider_type:"$p_type" \
    --tc=source_provider_version:"$p_version" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-$provider_type" \
    --tc=remote_ocp_cluster:"$remote_cluster" >$log_dir/$p_type-$p_version-$storage_type-remote.xml
fi

# Matrix local
if [[ "$test_scope" == "matrix" && "$test_type" == "local" && -n "$local_cluster" ]]; then
  echo "Run matrix local testing: $p_type-$p_version-$storage_type"
  uv run pytest -m tier0 \
    --tc=matrix_test:true \
    --tc=storage_class:"$storage_type" \
    --tc=source_provider_type:"$p_type" \
    --tc=source_provider_version:"$p_version" \
    --tc=insecure_verify_skip:"true" \
    --tc=target_namespace:"mtv-api-tests-$provider_type" >$log_dir/$p_type-$p_version-$storage_type.xml
fi
