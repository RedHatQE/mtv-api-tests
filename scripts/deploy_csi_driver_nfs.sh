#!/bin/bash

set -ex

# Please configure these parameters first
kube_config_root_dir=""
cluster_name=""
nfs_server_ip=""  #f02-h06-000-r640.rdu2.scalelab.redhat.com
nfs_share=""      #/home/nfsshare

export KUBECONFIG=$kube_config_root_dir/$cluster_name/auth/kubeconfig
oc login -u kubeadmin -p $(cat $kube_config_root_dir/$cluster_name/auth/kubeadmin-password)

# Install latest csi-driver-nfs
curl -O https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/v4.1.0/deploy/install-driver.sh
sed -i 's/kubectl/oc/g' install-driver.sh
source ./install-driver.sh v4.1.0

cat << EOF | oc apply -f -
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: nfs-csi
provisioner: nfs.csi.k8s.io
parameters:
  server: ${nfs_server_ip}
  share: ${nfs_share}
  # csi.storage.k8s.io/provisioner-secret is only needed for providing mountOptions in DeleteVolume
  csi.storage.k8s.io/provisioner-secret-name: "mount-options"
  csi.storage.k8s.io/provisioner-secret-namespace: "default"
  mountPermissions: '0777'
reclaimPolicy: Delete
volumeBindingMode: Immediate
mountOptions:
---
kind: Secret
apiVersion: v1
metadata:
  name: mount-options
  namespace: default
data:
  mountOptions: bmZzdmVycz00LjE=
type: Opaque
EOF
