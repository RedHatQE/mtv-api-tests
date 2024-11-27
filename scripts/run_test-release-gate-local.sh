#!/bin/bash

# Please configure these parameters first!
kube_config_root_dir=""
local_cluster=""

mountpoint $kube_config_root_dir
if [[ $? != 0 ]]; then
  echo "$kube_config_root_dir is not mounted with rhos_psi_cluster_dirs. Please mount it first."
  sudo -S mount -t nfs 10.9.96.21:/rhos_psi_cluster_dirs $kube_config_root_dir
fi

local_kube_config=$kube_config_root_dir/$local_cluster/auth/kubeconfig
export KUBECONFIG=$local_kube_config

################## vsphere #######################
uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
  --tc=source_provider_type:"vsphere" \
  --tc=source_provider_version:"7.0.3" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-vsphere" >../rp-uploader/vsphere_ceph-rbd.xml

uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"nfs-csi" \
  --tc=source_provider_type:"vsphere" \
  --tc=source_provider_version:"8.0.1" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-vsphere" >../rp-uploader/vsphere_nfs-csi.xml
