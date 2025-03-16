#! /usr/bin/env bash

# Function to display usage
usage() {
  echo "Usage: $0 <cluster-name>"
  exit 1
}

# Check if an argument is provided
if [ "$#" -lt 1 ]; then
  usage
fi

CLUSTER_NAME=$1

MOUNT_PATH="/mnt/cnv-qe.rhcloud.com"
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
