#!/bin/bash

# Please configure these parameters first!
kube_config_root_dir=""
local_cluster=""
remote_cluster=""

if ! mountpoint "$kube_config_root_dir"; then
  echo "$kube_config_root_dir is not mounted with rhos_psi_cluster_dirs. Please mount it first."
  sudo -S mount -t nfs 10.9.96.21:/rhos_psi_cluster_dirs "$kube_config_root_dir"
fi

local_kube_config=$kube_config_root_dir/$local_cluster/auth/kubeconfig
export KUBECONFIG=$local_kube_config

############### ovirt ####################
uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"nfs-csi" \
  --tc=source_provider_type:"ovirt" \
  --tc=source_provider_version:"4.4.9" \
  --tc=target_namespace:"mtv-api-tests-ovirt" >../rp-uploader/ovirt_nfs-csi.xml

uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
  --tc=source_provider_type:"ovirt" \
  --tc=source_provider_version:"4.4.9" \
  --tc=target_namespace:"mtv-api-tests-ovirt" >../rp-uploader/ovirt_ceph-rbd.xml

uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"ovirt" \
  --tc=source_provider_version:"4.4.9" \
  --tc=target_namespace:"mtv-api-tests-ovirt" >../rp-uploader/ovirt_standard-csi.xml

uv run pytest -m remote \
  --tc=matrix_test:true \
  --tc=storage_class:"nfs-csi" \
  --tc=source_provider_type:"ovirt" \
  --tc=source_provider_version:"4.4.9" \
  --tc=target_namespace:"mtv-api-tests-ovirt" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/ovirt_remote_nfs-csi.xml

uv run pytest -m remote \
  --tc=matrix_test:true \
  --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
  --tc=source_provider_type:"ovirt" \
  --tc=source_provider_version:"4.4.9" \
  --tc=target_namespace:"mtv-api-tests-ovirt" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/ovirt_remote_ceph-rbd.xml

uv run pytest -m remote \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"ovirt" \
  --tc=source_provider_version:"4.4.9" \
  --tc=target_namespace:"mtv-api-tests-ovirt" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/ovirt_remote_standard-csi.xml

################## vsphere #######################
uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"nfs-csi" \
  --tc=source_provider_type:"vsphere" \
  --tc=source_provider_version:"6.5" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-vsphere" >../rp-uploader/vsphere_nfs-csi.xml

uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
  --tc=source_provider_type:"vsphere" \
  --tc=source_provider_version:"6.5" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-vsphere" >../rp-uploader/vsphere_ceph-rbd.xml

uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"vsphere" \
  --tc=source_provider_version:"6.5" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-vsphere" >../rp-uploader/vsphere_standard-csi.xml

uv run pytest -m remote \
  --tc=matrix_test:true \
  --tc=storage_class:"nfs-csi" \
  --tc=source_provider_type:"vsphere" \
  --tc=source_provider_version:"6.5" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-vsphere" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/vsphere_remote_nfs-csi.xml

uv run pytest -m remote \
  --tc=matrix_test:true \
  --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
  --tc=source_provider_type:"vsphere" \
  --tc=source_provider_version:"6.5" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-vsphere" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/vsphere_remote_ceph-rbd.xml

uv run pytest -m remote \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"vsphere" \
  --tc=source_provider_version:"6.5" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-vsphere" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/vsphere_remote_standard-csi.xml

################## openstack #######################
uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"nfs-csi" \
  --tc=source_provider_type:"openstack" \
  --tc=source_provider_version:"psi" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-openstack" >../rp-uploader/openstack_nfs-csi.xml

uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
  --tc=source_provider_type:"openstack" \
  --tc=source_provider_version:"psi" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-openstack" >../rp-uploader/openstack_ceph-rbd.xml

uv run pytest -m tier0 \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"openstack" \
  --tc=source_provider_version:"psi" \
  --tc=insecure_verify_skip:"true" \
  --tc=target_namespace:"mtv-api-tests-openstack" >../rp-uploader/openstack_standard-csi.xml

################## ocp-to-ocp #######################
uv run pytest -m ocp \
  --tc=matrix_test:true \
  --tc=storage_class:"nfs-csi" \
  --tc=source_provider_type:"openshift" \
  --tc=source_provider_version:"localhost" \
  --tc=target_namespace:"mtv-api-tests-ocp" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/ocp_nfs-csi.xml

uv run pytest -m ocp \
  --tc=matrix_test:true \
  --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
  --tc=source_provider_type:"openshift" \
  --tc=source_provider_version:"localhost" \
  --tc=target_namespace:"mtv-api-tests-ocp" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/ocp_ceph-rbd.xml

uv run pytest -m ocp \
  --tc=matrix_test:true \
  --tc=storage_class:"standard-csi" \
  --tc=source_provider_type:"openshift" \
  --tc=source_provider_version:"localhost" \
  --tc=target_namespace:"mtv-api-tests-ocp" \
  --tc=remote_ocp_cluster:"$remote_cluster" >../rp-uploader/ocp_standard-csi.xml

################## ova #######################
uv run pytest -m ova \
  --tc=matrix_test:true \
  --tc=storage_class:"ocs-storagecluster-ceph-rbd" \
  --tc=source_provider_type:"ova" \
  --tc=source_provider_version:"nfs" \
  --tc=target_namespace:"mtv-api-tests-ova" \
  --tc=insecure_verify_skip:"true" >../rp-uploader/ova_ceph-rbd.xml
