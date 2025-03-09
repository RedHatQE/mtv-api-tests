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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
CLUSTER_NAME=$1
PASSWORD=$("$SCRIPT_DIR"/get-cluster-admin-password.sh "$CLUSTER_NAME")

CMD="oc login https://api.$CLUSTER_NAME.rhos-psi.cnv-qe.rhood.us:6443 -u kubeadmin -p $PASSWORD"

echo "$CMD"

$CMD

CONSOLE=$(oc get console cluster -ojson | jq '.status.consoleURL')
echo "Console URL: $CONSOLE"
