#! /usr/bin/env bash

usage() {
  echo "Usage: $0 <cluster-name>"
  exit 1
}

# Check if an argument is provided
if [ "$#" -lt 1 ]; then
  usage
fi

CLUSTER_NAME=$1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
"$SCRIPT_DIR"/oc-login.sh "$CLUSTER_NAME"

oc patch storagecluster ocs-storagecluster -n openshift-storage --type json --patch '[{ "op": "replace", "path": "/spec/enableCephTools", "value": true }]'

for _ in $(seq 1 10); do
  TOOLS_POD=$(oc get pod -n openshift-storage | grep rook-ceph-tools | awk -F" " '{print$1}')
  if [ "$TOOLS_POD" != "" ]; then
    break
  else
    sleep 1
  fi
done

CEPH_POOL="ocs-storagecluster-cephblockpool"
POD_EXEC_CMD="oc exec -it -n openshift-storage $TOOLS_POD"
RBD_LIST=$($POD_EXEC_CMD -- rbd ls "$CEPH_POOL")

for SNAP in $RBD_LIST; do
  SNAP_PATH="$CEPH_POOL"/"$SNAP"
  $POD_EXEC_CMD -- rbd snap purge "$SNAP_PATH"
done
