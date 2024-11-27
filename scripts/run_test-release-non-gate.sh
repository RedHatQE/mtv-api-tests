#!/bin/bash

# Please configure these parameters first!
kube_config_root_dir=""
local_cluster=""
remote_cluster=""

mountpoint $kube_config_root_dir
if [[ $? != 0 ]]; then
  echo "$kube_config_root_dir is not mounted with rhos_psi_cluster_dirs. Please mount it first."
  sudo -S mount -t nfs 10.9.96.21:/rhos_psi_cluster_dirs $kube_config_root_dir
fi

local_kube_config=$kube_config_root_dir/$local_cluster/auth/kubeconfig
export KUBECONFIG=$local_kube_config

############### ovirt ####################
uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"ovirt" \
  --tc=source_provider_version:"4.4.9" \
  --tc=target_namespace:"mtv-api-tests-ovirt" >../rp-uploader/ovirt_standard-csi.xml

uv run pytest -m remote \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"ovirt" \
  --tc=source_provider_version:"4.4.9" \
  --tc=target_namespace:"mtv-api-tests-ovirt" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/ovirt_remote_standard-csi.xml

################## openstack #######################
uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"openstack" \
  --tc=source_provider_version:"psi" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-openstack" >../rp-uploader/openstack_standard-csi.xml
