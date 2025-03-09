#! /usr/bin/env bash

CLUSTER_NAME=$1
export CLUSTER_NAME
shift 1

MOUNT_PATH="/mnt/cnv-qe.rhcloud.com"
export MOUNT_PATH
CLUSTER_MOUNT_PATH="$MOUNT_PATH/$CLUSTER_NAME"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

cmd=$(uv run "$SCRIPT_DIR"/build_run_tests_command.py "$@")
if [ $? -ne 0 ]; then
  echo "$cmd"
  exit 1
fi

echo "$cmd"

if [ ! -d "$CLUSTER_MOUNT_PATH" ]; then
  sudo mount -t nfs 10.9.96.21:/rhos_psi_cluster_dirs "$MOUNT_PATH"
fi

if [ ! -d "$CLUSTER_MOUNT_PATH" ]; then
  echo "Mount path $CLUSTER_MOUNT_PATH does not exist. Exiting."
  exit 1
fi

CLUSTER_FILES_PATH="$MOUNT_PATH/$CLUSTER_NAME/auth"
KUBECONFIG_FILE="$CLUSTER_FILES_PATH/kubeconfig"
PASSWORD_FILE="$CLUSTER_FILES_PATH/kubeadmin-password"

if [ ! -f "$KUBECONFIG_FILE" ] || [ ! -f "$PASSWORD_FILE" ]; then
  echo "Missing kubeconfig or password file. Exiting."
  exit 1
fi

PASSWORD_CONTENT=$(cat "$PASSWORD_FILE")

export KUBECONFIG=$KUBECONFIG_FILE
export OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG

oc login https://api."$CLUSTER_NAME".rhos-psi.cnv-qe.rhood.us:6443 -u kubeadmin -p "$PASSWORD_CONTENT"

$cmd
