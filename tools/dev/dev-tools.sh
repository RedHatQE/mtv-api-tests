#! /usr/bin/env bash

SUPPORTED_ACTIONS='''
Supported actions:
  cluster-password
  cluster-login
  run-tests
  mtv-resources
  ceph-cleanup
  ceph-df [--watch]
  list-clusters
  csi-nfs-df
'''
# Function to display usage
usage() {
  printf "Usage: %s <action> [<cluster-name>]\n" "$0"
  printf "%s" "$SUPPORTED_ACTIONS"
  exit 1
}

ACTION=$1
CLUSTER_NAME=$2
MOUNT_PATH="/mnt/cnv-qe.rhcloud.com"
export MOUNT_PATH
export CLUSTER_NAME

cluster-password() {
  if [ -z "$CLUSTER_NAME" ]; then
    echo "Cluster name is required. Exiting."
    usage
  fi
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
  if [ -z "$CLUSTER_NAME" ]; then
    echo "Cluster name is required. Exiting."
    usage
  fi

  PASSWORD=$(cluster-password)
  if [[ $? != 0 ]]; then
    echo "Password for $CLUSTER_NAME not found. Exiting."
    exit 1
  fi

  USERNAME="kubeadmin"

  CMD="oc login --insecure-skip-tls-verify=true https://api.$CLUSTER_NAME.rhos-psi.cnv-qe.rhood.us:6443 -u $USERNAME -p $PASSWORD"

  loggedin=$(timeout 5s oc whoami &>/dev/null)
  if [[ $? == 0 ]]; then
    loggedin=0
  else
    loggedin=1
  fi
  loggedinsameserver=$(oc whoami --show-server | grep -c "$CLUSTER_NAME" &>/dev/null)
  if [[ $? == 0 ]]; then
    loggedinsameserver=0
  else
    loggedinsameserver=1
  fi

  if [[ $loggedin == 0 && $loggedinsameserver == 0 ]]; then
    printf "Already logged in to %s\n\n" "$CLUSTER_NAME"
  else
    timeout 5s oc logout &>/dev/null
    $CMD &>/dev/null
    # loggedin=$(oc whoami &>/dev/null)
    if ! oc whoami &>/dev/null; then
      echo "Failed to login to $CLUSTER_NAME. Exiting."
      exit 1
    fi
  fi

  CONSOLE=$(oc get console cluster -o jsonpath='{.status.consoleURL}')
  MTV_VERSION=$(oc get csv -n openshift-mtv -o jsonpath='{.items[*].spec.version}')
  CNV_VERSION=$(oc get csv -n openshift-cnv -o jsonpath='{.items[*].spec.version}')
  OCP_VERSION=$(oc get clusterversion -o jsonpath='{.items[*].status.desired.version}')
  IIB=$(oc get catalogsource -n openshift-marketplace --sort-by='metadata.creationTimestamp' | grep redhat-osbs- | tail | awk '{print$1}')

  format_string="Username: %s\nPassword: %s\nLogin: %s\nConsole: %s\nOCP version: %s\nMTV version: %s (%s)\nCNV version: %s\n\n"
  printf -v res "$format_string" \
    "$USERNAME" \
    "$PASSWORD" \
    "$CMD" \
    "$CONSOLE" \
    "$OCP_VERSION" \
    "$MTV_VERSION" \
    "$IIB" \
    "$CNV_VERSION"

  print-cluster-data-tree "$res"

  XSEL_EXISTS=$(command -v xsel &>/dev/null)
  if ${XSEL_EXISTS}; then
    xsel -bi <<<"$PASSWORD"
    printf "Password copied to clipboard.\n"
  fi
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

  # export KUBECONFIG=$KUBECONFIG_FILE
  export OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG

  $cmd
}

enable-ceph-tools() {
  cluster-login
  oc patch storagecluster ocs-storagecluster -n openshift-storage --type json --patch '[{ "op": "replace", "path": "/spec/enableCephTools", "value": true }]' &>/dev/null

  TOOLS_POD=$(oc get pods -n openshift-storage -l app=rook-ceph-tools -o name)
}

ceph-df() {
  enable-ceph-tools

  POD_EXEC_CMD="oc exec -n openshift-storage $TOOLS_POD -- ceph df"
  if [[ $3 == "--watch" ]]; then
    watch -n 10 "$POD_EXEC_CMD"
  else
    DF=$($POD_EXEC_CMD)
    printf "%s" "$DF"
  fi
}

ceph-cleanup() {
  enable-ceph-tools
  local POD_EXEC_CMD="oc exec -n openshift-storage $TOOLS_POD"
  local CEPH_POOL="ocs-storagecluster-cephblockpool"
  local logged_commands=""

  local RBD_LIST
  local SNAP_AND_VOL
  local SNAP_AND_VOL_PATH
  local RBD_TRASH_LIST
  local TRASH
  local TRASH_ITEM_PATH

  logged_commands+="$POD_EXEC_CMD -- ceph osd set-full-ratio 0.90"$'\n'

  RBD_LIST=$($POD_EXEC_CMD -- rbd ls "$CEPH_POOL")
  for SNAP_AND_VOL in $RBD_LIST; do
    SNAP_AND_VOL_PATH="$CEPH_POOL/$SNAP_AND_VOL"
    if grep -q "snap" <<<"$SNAP_AND_VOL"; then
      logged_commands+="$POD_EXEC_CMD -- rbd snap purge $SNAP_AND_VOL_PATH"$'\n'
    fi
    if grep -q "vol" <<<"$SNAP_AND_VOL"; then
      logged_commands+="$POD_EXEC_CMD -- rbd rm $SNAP_AND_VOL_PATH"$'\n'
    fi
  done

  RBD_TRASH_LIST=$($POD_EXEC_CMD -- rbd trash list "$CEPH_POOL" | awk -F" " '{print$1}')
  for TRASH in $RBD_TRASH_LIST; do
    TRASH_ITEM_PATH="$CEPH_POOL/$TRASH"
    logged_commands+="$POD_EXEC_CMD -- rbd trash remove $TRASH_ITEM_PATH"$'\n'
  done

  logged_commands+="$POD_EXEC_CMD -- ceph osd set-full-ratio 0.85"$'\n'
  logged_commands+="$POD_EXEC_CMD -- ceph df"$'\n'

  if [ -n "$logged_commands" ]; then
    printf "%s" "$logged_commands"
  fi
  XSEL_EXISTS=$(command -v xsel &>/dev/null)
  if ${XSEL_EXISTS}; then
    xsel -bi <<<"$logged_commands"
    printf "Content copied to clipboard.\n"
  fi
}

list-clusters() {
  for cluster_path in "$MOUNT_PATH"/qemtv-*; do
    export CLUSTER_NAME="${cluster_path##*/}"
    res=$(cluster-login)
    process_this_data_block=true

    if [[ "$res" == "Failed to login"* ]]; then
      process_this_data_block=false
    fi

    if [ "$process_this_data_block" = true ]; then
      print-cluster-data-tree "$res"

    fi
  done
}

print-cluster-data-tree() {
  res=$1
  filtered_data=$(echo "$res" | grep -v "Password copied to clipboard")
  num_lines=$(echo "$filtered_data" | wc -l | awk '{print $1}')

  # Print a root label for your tree
  echo "OpenShift Cluster Info -- [$CLUSTER_NAME]"

  # Process the data with awk to print in a tree structure
  echo "$filtered_data" | awk -v total_lines="$num_lines" '
    BEGIN {
        # Define the field separator for parsing key and value.
        # This separates on the first occurrence of ": ".
        FS = ": "
        OFS = ": " # Output field separator
    }
    {
        # Extract the key (everything before the first ": ")
        key = $1

        # Extract the value (everything after the first ": ")
        # This handles cases where the value itself might contain colons.
        value_start = index($0, ": ") + 2 # Find start of value
        value = substr($0, value_start)  # Extract value

        # Determine the prefix based on whether it is the last line
        if (NR < total_lines) {
            prefix = "├── "
        } else {
            prefix = "└── "
        }

        # Print the formatted line
        print prefix key OFS value
    }'
  echo ""
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
elif [ "$ACTION" == "ceph-df" ]; then
  ceph-df "$@"
elif [ "$ACTION" == "list-clusters" ]; then
  list-clusters
elif [ "$ACTION" == "csi-nfs-df" ]; then
  cluster-login
  "$SCRIPT_DIR/check_nfs_csi_space.sh"
else
  printf "Unsupported action: %s\n" "$ACTION"
  usage
fi
