#!/bin/bash

if [ -z $1 ] || [ -z $2 ]
then
  echo ""
  echo "Usage: $0 [PV_COUNT] [PV_SIZE]"
  echo "Example $0 100 50 (100 PVs , 50GB size)"
  echo "Abort script!!!"
  exit 1
fi

PV_COUNT=$1
PV_SIZE=$2

# Login
export KUBECONFIG="/home/kni/clusterconfigs/auth/kubeconfig"
oc login -u kubeadmin -p $(cat /home/kni/clusterconfigs/auth/kubeadmin-password) -n default

export CLUSTER_NAME=`oc get routes -nopenshift-console | grep downloads | awk '{print $2}' | tr -s . " " | awk '{print $3}'`
export CLUSTER_DOMAIN=`oc get routes -nopenshift-console | grep downloads | awk '{print $2}' | tr -s . " " | awk '{print $4}'`

readonly NFS_SERVER=`hostname`
readonly NFS_FOLDER="/Nvme_Disk"

PV_PREFIX="scalenfs-pv"

PV_DIR="${NFS_FOLDER}/${CLUSTER_NAME}.${CLUSTER_DOMAIN}.pvs"
sudo mkdir -vp "${PV_DIR}"

pv_count=`oc get pv |grep ${PV_PREFIX} |wc -l`

for pv_dir in `seq 1 ${pv_count}`
do
  oc delete pv "${PV_PREFIX}$pv_dir" --ignore-not-found
  sudo rm -rf "${PV_DIR}/${PV_PREFIX}$pv_dir"
done

# remove old one if exists
oc delete sc nfs || true

# create storageclass
cat > nfs-sc.yml << __EOF__
---
kind: StorageClass
apiVersion: storage.k8s.io/v1
metadata:
  name: nfs
spec:
  claimPropertySets:
  - accessModes:
    - ReadWriteOnce
    volumeMode: Filesystem
provisioner: kubernetes.io/no-provisioner
reclaimPolicy: Delete
__EOF__

oc create -f nfs-sc.yml

# Create  PVs
for i in `seq 1 ${PV_COUNT}`
do
  sudo mkdir -p ${PV_DIR}"/"${PV_PREFIX}$i
oc create -f - << __EOF__
---
kind: PersistentVolume
apiVersion: v1
metadata:
  name: ${PV_PREFIX}${i}
spec:
  storageClassName: nfs
  capacity:
    storage: ${PV_SIZE}Gi
  accessModes:
   - ReadWriteMany
   - ReadWriteOnce
  nfs:
    path: ${PV_DIR}/${PV_PREFIX}${i}
    server: ${NFS_SERVER}
  persistentVolumeReclaimPolicy: Retain
__EOF__
done

oc patch storageprofile/nfs --type merge --patch '{"spec":{"claimPropertySets":[{"accessModes":["ReadWriteOnce"],"volumeMode":"Filesystem"}]}}'
