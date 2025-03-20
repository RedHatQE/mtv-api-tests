#! /usr/bin/env bash

SUPPORTED_ACTIONS='''
Supported actions:
  cluster-password
  cluster-login
  run-tests
  mtv-resources
  ceph-cleanup
'''
# Function to display usage
usage() {
  printf "Usage: %s <cluster-name> <action>\n" "$0"
  printf "%s" "$SUPPORTED_ACTIONS"
  exit 1
}

# Check if an argument is provided
if [ "$#" -lt 2 ]; then
  usage
fi

CLUSTER_NAME=$1
ACTION=$2
MOUNT_PATH="/mnt/cnv-qe.rhcloud.com"
export MOUNT_PATH
export CLUSTER_NAME

cluster-password() {
  export MOUNT_PATH

  CLUSTER_MOUNT_PATH="$MOUNT_PATH/$CLUSTER_NAME"

  if [ ! -d "$MOUNT_PATH" ]; then
    sudo mkdir -p "$MOUNT_PATH"
  fi

  if [ ! -d "$CLUSTER_MOUNT_PATH" ]; then
    sudo mount -t nfs 10.9.96.21:/rhos_psi_cluster_dirs "$MOUNT_PATH"
  fi

  if [ ! -d "$CLUSTER_MOUNT_PATH" ]; then
    echo "Mount path $CLUSTER_MOUNT_PATH does not exist. Exiting."
    exit 1
  fi

  CLUSTER_FILES_PATH="$MOUNT_PATH/$CLUSTER_NAME/auth"
  PASSWORD_FILE="$CLUSTER_FILES_PATH/kubeadmin-password"

  if [ ! -f "$PASSWORD_FILE" ]; then
    echo "Missing password file. Exiting."
    exit 1
  fi

  PASSWORD_CONTENT=$(cat "$PASSWORD_FILE")
  echo "$PASSWORD_CONTENT"
}

cluster-login() {
  oc logout &>/dev/null
  PASSWORD=$(cluster-password)
  USERNAME="kubeadmin"

  CMD="oc login --insecure-skip-tls-verify=true https://api.$CLUSTER_NAME.rhos-psi.cnv-qe.rhood.us:6443 -u $USERNAME -p $PASSWORD"
  $CMD &>/dev/null

  printf "Username: %s\nPassword: %s\nLogin: %s\nConsole: %s\n\n" "$USERNAME" "$PASSWORD" "$CMD" "$(oc get console cluster -ojson | jq -r '.status.consoleURL')"
}

mtv-resources() {
  cluster-login
  RESOUECES="ns pods dv pvc pv plan migration storagemap networkmap provider host secret net-attach-def hook vm vmi"
  for resource in $RESOUECES; do
    res=$(oc get "$resource" -A | grep mtv-api)
    IFS=$'\n' read -r -d '' -a array <<<"$res"

    echo "$resource:"
    for line in "${array[@]}"; do
      echo "    $line"
    done
    echo -e '\n'
  done
}

run-tests() {
  cluster-login
  shift 2

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

  cmd=$(uv run "$SCRIPT_DIR"/build_run_tests_command.py "$@")
  if [ $? -ne 0 ]; then
    echo "$cmd"
    exit 1
  fi

  echo "$cmd"

  KUBECONFIG_FILE="$MOUNT_PATH/$CLUSTER_NAME/auth/kubeconfig"

  if [ ! -f "$KUBECONFIG_FILE" ]; then
    echo "Missing kubeconfig file. Exiting."
    exit 1
  fi

  export KUBECONFIG=$KUBECONFIG_FILE
  export OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG

  $cmd
}

ceph-cleanup() {
  cluster-login
  oc patch storagecluster ocs-storagecluster -n openshift-storage --type json --patch '[{ "op": "replace", "path": "/spec/enableCephTools", "value": true }]' &>/dev/null

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
    SNAP_PATH="$CEPH_POOL/$SNAP"
    echo "$POD_EXEC_CMD" -- rbd snap purge "$SNAP_PATH"
  done
}

if [ "$ACTION" == "cluster-password" ]; then
  cluster-password
elif [ "$ACTION" == "cluster-login" ]; then
  cluster-login
elif [ "$ACTION" == "mtv-resources" ]; then
  mtv-resources
elif [ "$ACTION" == "run-tests" ]; then
  run-tests "$@"
elif [ "$ACTION" == "ceph-cleanup" ]; then
  ceph-cleanup
else
  printf "Unsupported action: %s\n%s" "$ACTION" "$SUPPORTED_ACTIONS"
fi
