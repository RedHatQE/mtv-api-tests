#! /usr/bin/env bash

CLUSTER_NAME=$1
export CLUSTER_NAME
shift 1

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

"$SCRIPT_DIR"/oc-login.sh "$CLUSTER_NAME"

$cmd
