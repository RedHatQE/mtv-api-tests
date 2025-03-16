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
USERNAME="kubeadmin"

CMD="oc login https://api.$CLUSTER_NAME.rhos-psi.cnv-qe.rhood.us:6443 -u $USERNAME -p $PASSWORD"

if oc whoami &>/dev/null; then
  if oc whoami --show-server | grep "$CLUSTER_NAME" &>/dev/null; then
    printf "Already logged in to %s\n\n" "$CLUSTER_NAME"
  fi
else
  $CMD &>/dev/null
fi

printf "Username: %s\nPassword: %s\nLogin: %s\nConsole: %s\n" "$USERNAME" "$PASSWORD" "$CMD" "$(oc get console cluster -ojson | jq -r '.status.consoleURL')"
# printf "Password: $PASSWORD"
# printf "Login: $CMD"
# printf "Console: $(oc get console cluster -ojson | jq -r '.status.consoleURL')"
