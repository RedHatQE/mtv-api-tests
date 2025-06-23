#! /usr/bin/env bash

set -euo pipefail

# Colors
C_RED='\033[0;31m'
C_GREEN='\033[0;32m'
C_YELLOW='\033[0;33m'
C_BLUE='\033[0;34m'
C_BOLD='\033[1m'
C_RESET='\033[0m'

usage() {
  printf "A collection of tools for managing OpenShift clusters for MTV testing.\n\n"
  printf "${C_BOLD}Usage:${C_RESET}\n"
  printf "  %s <command> [arguments]\n\n" "$(basename "$0")"
  printf "${C_BOLD}Global options:${C_RESET}\n"
  printf "  ${C_GREEN}--help${C_RESET}          Show this help message and exit.\n\n"
  printf "${C_BOLD}Available commands:${C_RESET}\n"
  printf "  ${C_GREEN}cluster-password <cluster-name>${C_RESET}\n"
  printf "      Prints the kubeadmin password for a cluster.\n\n"
  printf "  ${C_GREEN}cluster-login <cluster-name>${C_RESET}\n"
  printf "      Logs into a cluster and prints its details. Copies password to clipboard.\n\n"
  printf "  ${C_GREEN}run-tests <cluster-name> [test-args...]${C_RESET}\n"
  printf "      Runs the API tests against a specified cluster.\n\n"
  printf "  ${C_GREEN}mtv-resources <cluster-name>${C_RESET}\n"
  printf "      Lists MTV-related resources in a cluster.\n\n"
  printf "  ${C_GREEN}ceph-cleanup <cluster-name> [--execute]${C_RESET}\n"
  printf "      Generates commands to clean up Ceph resources. Use --execute to run them.\n\n"
  printf "  ${C_GREEN}ceph-df <cluster-name> [--watch]${C_RESET}\n"
  printf "      Displays Ceph cluster usage. Use --watch to monitor in real-time.\n\n"
  printf "  ${C_GREEN}list-clusters [--full]${C_RESET}\n"
  printf "      Lists available clusters. By default, shows a summary. Use --full for details.\n\n"
  printf "  ${C_GREEN}generate-completion-script <bash|zsh>${C_RESET}\n"
  printf "      Generates a completion script for the specified shell.\n"
  printf "      To install, add the relevant line to your shell's startup file:\n"
  printf "      ${C_YELLOW}Bash: source <(./tools/dev/dev-tools.sh generate-completion-script bash)${C_RESET}\n"
  printf "      ${C_YELLOW}Zsh:  source <(./tools/dev/dev-tools.sh generate-completion-script zsh)${C_RESET}\n\n"
  printf "  ${C_GREEN}csi-nfs-df <cluster-name>${C_RESET}\n"
  printf "      Checks the available space on the NFS CSI driver.\n"
  exit 0
}

ensure_nfs_mounted() {
  if ! findmnt -n -T "$MOUNT_PATH" >/dev/null; then
    if [ ! -d "$MOUNT_PATH" ]; then
      echo "Creating mount path: $MOUNT_PATH" >&2
      sudo mkdir -p "$MOUNT_PATH"
    fi
    echo "Mounting NFS share..." >&2
    sudo mount -t nfs 10.9.96.21:/rhos_psi_cluster_dirs "$MOUNT_PATH"
  fi
}

MOUNT_PATH="/mnt/cnv-qe.rhcloud.com"
export MOUNT_PATH

cluster-password() {
  local cluster_name="$1"
  local copy_to_clipboard=true
  if [[ "${2-}" == "--no-copy" ]]; then
    copy_to_clipboard=false
  fi

  if [ -z "$cluster_name" ]; then
    echo -e "${C_RED}Error: Cluster name is required.${C_RESET}" >&2
    usage
  fi

  ensure_nfs_mounted

  local cluster_mount_path="$MOUNT_PATH/$cluster_name"

  if [ ! -d "$cluster_mount_path" ]; then
    echo "Mount path $cluster_mount_path does not exist after mount attempt. Exiting." >&2
    exit 1
  fi

  local password_file="$cluster_mount_path/auth/kubeadmin-password"

  if [ ! -f "$password_file" ]; then
    echo "Missing password file: $password_file. Exiting." >&2
    exit 1
  fi

  local password
  password=$(cat "$password_file")
  echo "$password"

  if [ "$copy_to_clipboard" = true ] && command -v xsel &>/dev/null; then
    xsel -bi <<<"$password"
    printf "Password copied to clipboard.\n" >&2
  fi
}

cluster-login() {
  local cluster_name="$1"
  local copy_to_clipboard=true
  if [[ "${2-}" == "--no-copy" ]]; then
    copy_to_clipboard=false
  fi

  if [ -z "$cluster_name" ]; then
    echo -e "${C_RED}Error: Cluster name is required.${C_RESET}" >&2
    usage
  fi

  local username="kubeadmin"
  local password
  password=$(cluster-password "$cluster_name" --no-copy)
  if [ -z "$password" ]; then
    exit 1
  fi

  local api_url="https://api.$cluster_name.rhos-psi.cnv-qe.rhood.us:6443"
  local login_cmd="oc login --insecure-skip-tls-verify=true $api_url -u $username -p $password"

  # Redirect stderr to /dev/null to suppress connection errors when not logged in
  if ! oc whoami --show-server 2>/dev/null | grep -q "$api_url"; then
    echo "Logging in to $cluster_name..." >&2
    if ! oc login --insecure-skip-tls-verify=true "$api_url" -u "$username" -p "$password" >/dev/null; then
      echo -e "${C_RED}Error: Failed to log in to cluster '$cluster_name'. Please check cluster name and connectivity.${C_RESET}" >&2
      exit 1
    fi
  else
    printf "Already logged in to %s\n\n" "$cluster_name" >&2
  fi

  if [ "$copy_to_clipboard" = true ] && command -v xsel &>/dev/null; then
    xsel -bi <<<"$password"
    printf "Password copied to clipboard.\n\n" >&2
  fi

  local console
  console=$(oc get console cluster -o jsonpath='{.status.consoleURL}')
  local mtv_version
  mtv_version=$(oc get csv -n openshift-mtv -o jsonpath='{.items[*].spec.version}')
  local cnv_version
  cnv_version=$(oc get csv -n openshift-cnv -o jsonpath='{.items[*].spec.version}')
  local ocp_version
  ocp_version=$(oc get clusterversion -o jsonpath='{.items[*].status.desired.version}')
  local iib
  iib=$(oc get catalogsource -n openshift-marketplace --sort-by='metadata.creationTimestamp' | grep redhat-osbs- | tail -n 1 | awk '{print$1}')

  if [ "${3-}" == "--get-version" ]; then
      echo "$ocp_version"
      return
  fi

  local format_string="Username: %s\nPassword: %s\nLogin: %s\nConsole: %s\nOCP version: %s\nMTV version: %s (%s)\nCNV version: %s\n"
  printf "$format_string" \
    "$username" \
    "$password" \
    "$login_cmd" \
    "$console" \
    "$ocp_version" \
    "$mtv_version" \
    "$iib" \
    "$cnv_version"
}

mtv-resources() {
  local cluster_name="$1"
  cluster-login "$cluster_name" >/dev/null

  local resources="ns pods dv pvc pv plan migration storagemap networkmap provider host secret net-attach-def hook vm vmi"
  for resource in $resources; do
    local res
    res=$(oc get "$resource" -A 2>/dev/null | grep 'mtv-api' || true)

    if [ -n "$res" ]; then
      echo "$resource:"
      # Use a while loop to indent each line of the output
      while IFS= read -r line; do
        echo "    $line"
      done <<<"$res"
      echo
    fi
  done
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

run-tests() {
  local cluster_name="$1"
  export CLUSTER_NAME="$cluster_name"
  shift # Remove cluster name from args
  local ocp_version
  ocp_version=$(cluster-login "$cluster_name" --no-copy --get-version)

  local cmd
  cmd=$(uv run "$SCRIPT_DIR"/build_run_tests_command.py --cluster-version="$ocp_version" "$@")
  echo "Running command:" >&2
  echo "$cmd"

  export OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL=DEBUG
  eval "$cmd"
}

enable-ceph-tools() {
  local cluster_name="$1"
  cluster-login "$cluster_name" --no-copy >/dev/null
  local tools_enabled
  tools_enabled=$(oc get storagecluster ocs-storagecluster -n openshift-storage -o jsonpath='{.spec.enableCephTools}' 2>/dev/null)

  if [ "$tools_enabled" != "true" ]; then
    echo "Enabling Ceph tools..." >&2
    oc patch storagecluster ocs-storagecluster -n openshift-storage --type json --patch '[{ "op": "replace", "path": "/spec/enableCephTools", "value": true }]' >/dev/null
  fi

  echo "Waiting for Ceph tools pod..." >&2
  local tools_pod
  local status
  for _ in $( # ~2.5 min timeout
    seq 1 30
  ); do
    tools_pod=$(oc get pods -n openshift-storage -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [ -n "$tools_pod" ]; then
      status=$(oc get pod "$tools_pod" -n openshift-storage -o jsonpath='{.status.phase}' 2>/dev/null)
      if [ "$status" == "Running" ]; then
        echo "$tools_pod"
        return 0
      fi
    fi
    sleep 5
  done

  echo "Error: Timed out waiting for Ceph tools pod to become ready." >&2
  return 1
}

ceph-df() {
  local cluster_name="$1"
  shift # consume cluster name

  if [ -n "${1-}" ] && [ "$1" != "--watch" ]; then
    echo -e "${C_RED}Error: Unknown argument '$1' for ceph-df.${C_RESET}" >&2
    usage
  fi

  local tools_pod
  tools_pod=$(enable-ceph-tools "$cluster_name")

  local pod_exec_cmd="oc exec -n openshift-storage $tools_pod -- ceph df"

  if [[ "${1-}" == "--watch" ]]; then
    watch -n 10 "$pod_exec_cmd"
  else
    $pod_exec_cmd
  fi
}

ceph-cleanup() {
  local cluster_name="$1"
  shift

  if [ -n "${1-}" ] && [ "$1" != "--execute" ]; then
    echo -e "${C_RED}Error: Unknown argument '$1' for ceph-cleanup.${C_RESET}" >&2
    usage
  fi

  local execute=false
  if [[ "${1-}" == "--execute" ]]; then
    execute=true
  fi

  local tools_pod
  tools_pod=$(enable-ceph-tools "$cluster_name")

  local pod_exec_cmd="oc exec -i -n openshift-storage $tools_pod" # Added -i for stdin
  local ceph_pool="ocs-storagecluster-cephblockpool"
  local logged_commands=""

  logged_commands+="$pod_exec_cmd -- ceph osd set-full-ratio 0.90"$'\n'

  local rbd_list
  rbd_list=$($pod_exec_cmd -- rbd ls "$ceph_pool")
  for image in $rbd_list; do
    local image_path="$ceph_pool/$image"
    # Purge all snapshots for the image.
    logged_commands+="$pod_exec_cmd -- rbd snap purge $image_path"$'\n'
    # Remove the image itself.
    logged_commands+="$pod_exec_cmd -- rbd rm $image_path"$'\n'
  done

  local rbd_trash_list
  rbd_trash_list=$($pod_exec_cmd -- rbd trash list "$ceph_pool" | awk -F" " '{print$1}')
  for trash_id in $rbd_trash_list; do
    local trash_item_path="$ceph_pool/$trash_id"
    logged_commands+="$pod_exec_cmd -- rbd trash remove $trash_item_path"$'\n'
  done

  logged_commands+="$pod_exec_cmd -- ceph osd set-full-ratio 0.85"$'\n'
  logged_commands+="$pod_exec_cmd -- ceph df"$'\n'

  if [ -z "$logged_commands" ]; then
    echo "No commands to execute."
    return
  fi

  if [ "$execute" = true ]; then
    echo "The following commands will be executed:"
    printf "%s\n" "$logged_commands"
    read -p "Are you sure you want to continue? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
      echo "Executing cleanup commands..."
      # We pipe the commands to bash for execution.
      # Using a subshell to avoid issues with variable scope and command execution.
      (
        IFS=$'\n'
        for cmd in $logged_commands; do
          echo "Running: $cmd"
          if ! eval "$cmd"; then
            echo "Warning: Command failed, but continuing execution: $cmd" >&2
          fi
        done
      )
      echo "Cleanup finished."
    else
      echo "Cleanup aborted by user."
    fi
  else
    echo "The following commands have been generated. Run with --execute to run them."
    printf "%s" "$logged_commands"
    if command -v xsel &>/dev/null; then
      xsel -bi <<<"$logged_commands"
      printf "\nContent copied to clipboard.\n"
    fi
  fi
}

list-clusters() {
  local full_info=false
  if [ -n "${1-}" ]; then
    if [[ "$1" == "--full" ]]; then
      full_info=true
    else
      echo -e "${C_RED}Error: Unknown argument '$1' for list-clusters${C_RESET}" >&2
      usage
    fi
  fi

  ensure_nfs_mounted

  for cluster_path in "$MOUNT_PATH"/qemtv{,d}-*; do
    local cluster_name="${cluster_path##*/}"
    local cluster_info
    if cluster_info=$(cluster-login "$cluster_name" --no-copy 2>/dev/null); then
      if [ "$full_info" = true ]; then
        print-cluster-data-tree "$cluster_info" "$cluster_name"
      else
        local ocp_version
        ocp_version=$(echo "$cluster_info" | grep "OCP version:" | sed 's/OCP version: //')
        local mtv_version
        mtv_version=$(echo "$cluster_info" | grep "MTV version:" | sed 's/MTV version: //')
        printf "%-20s OCP: %-15s MTV: %s\n" "$cluster_name" "$ocp_version" "$mtv_version"
      fi
    else
      echo -e "${C_RED}Could not log in to cluster: $cluster_name${C_RESET}" >&2
    fi
  done
}

print-cluster-data-tree() {
  local data="$1"
  local cluster_name="$2"
  # Filter out unwanted/empty lines before processing
  local filtered_data
  filtered_data=$(echo "$data" | grep -v "Password copied to clipboard" | grep -vE '^$')

  # Read filtered data into a bash array
  local lines=()
  while IFS= read -r line; do
    lines+=("$line")
  done <<<"$filtered_data"

  local num_lines=${#lines[@]}
  # Do nothing if there's no data to print
  if [ "$num_lines" -eq 0 ]; then
    return
  fi

  echo "OpenShift Cluster Info -- [$cluster_name]"

  # Loop through the array and print each line with tree characters
  for i in "${!lines[@]}"; do
    local line="${lines[$i]}"
    # Use bash parameter expansion to split key and value
    local key="${line%%: *}"
    local value="${line#*: }"

    local prefix="├──"
    if [ "$((i + 1))" -eq "$num_lines" ]; then
      prefix="└──"
    fi

    printf "%s %s: %s\n" "$prefix" "$key" "$value"
  done
  echo ""
}

generate_bash_completion_script() {
  local script_name
  script_name=$(basename "$0")

  cat <<EOF
# Bash completion for ${script_name}

_dev_tools_get_cluster_names()
{
    local MOUNT_PATH="/mnt/cnv-qe.rhcloud.com"
    if [ ! -d "\$MOUNT_PATH" ]; then
      return 1
    fi
    local clusters
    clusters=\$(ls -d "\$MOUNT_PATH"/qemtv{,d}-* 2>/dev/null | xargs -n 1 basename)
    COMPREPLY=( \$(compgen -W "\${clusters}" -- "\${cur}") )
}

_dev_tools_completion()
{
    local cur prev words cword
    _get_comp_words_by_ref -n : cur prev words cword

    local commands="cluster-password cluster-login run-tests mtv-resources ceph-cleanup ceph-df list-clusters csi-nfs-df --help generate-completion-script"

    if [ \${cword} -eq 1 ]; then
        COMPREPLY=( \$(compgen -W "\${commands}" -- "\${cur}") )
        return 0
    fi

    local current_command="\${words[1]}"

    case "\${current_command}" in
        list-clusters)
            if [[ "\${cur}" == --* ]]; then
                COMPREPLY=( \$(compgen -W "--full" -- "\${cur}") )
            fi
            ;;
        ceph-df)
            if [[ "\${cur}" == --* ]]; then
                COMPREPLY=( \$(compgen -W "--watch" -- "\${cur}") )
            elif [ \${cword} -eq 2 ]; then
                _dev_tools_get_cluster_names
            fi
            ;;
        ceph-cleanup)
            if [[ "\${cur}" == --* ]]; then
                COMPREPLY=( \$(compgen -W "--execute" -- "\${cur}") )
            elif [ \${cword} -eq 2 ]; then
                _dev_tools_get_cluster_names
            fi
            ;;
        generate-completion-script)
            if [ \${cword} -eq 2 ]; then
                COMPREPLY=( \$(compgen -W "bash zsh" -- "\${cur}") )
            fi
            ;;
        cluster-password|cluster-login|mtv-resources|run-tests|csi-nfs-df)
            if [ \${cword} -eq 2 ]; then
                _dev_tools_get_cluster_names
            fi
            ;;
    esac
}

complete -F _dev_tools_completion "${script_name}"
complete -F _dev_tools_completion "$(realpath "$0")"
EOF
}

generate_zsh_completion_script() {
  local script_name
  script_name=$(basename "$0")
  local script_path
  script_path=$(realpath "$0")

  cat <<EOF
#compdef _dev_tools ${script_name} ${script_path}

_dev_tools_get_cluster_names() {
    local MOUNT_PATH="/mnt/cnv-qe.rhcloud.com"
    if [ ! -d "\$MOUNT_PATH" ]; then return 1; fi
    local -a clusters
    clusters=(\${(f)"\$(ls -d \$MOUNT_PATH/qemtv{,d}-* 2>/dev/null | xargs -n 1 basename)"})
    _describe 'cluster' clusters
    return 0
}

_dev_tools() {
    local -a commands

    commands=(
      'cluster-password:Get cluster password'
      'cluster-login:Login to cluster'
      'run-tests:Run API tests'
      'mtv-resources:List MTV resources'
      'ceph-cleanup:Cleanup Ceph resources'
      'ceph-df:Show Ceph space usage'
      'list-clusters:List available clusters'
      'csi-nfs-df:Check NFS CSI space'
      'generate-completion-script:Generate completion script for bash or zsh'
      '--help:Show help message'
    )

    if (( CURRENT == 2 )); then
      _describe 'command' commands
      return
    fi

    if (( CURRENT == 3 )); then
      case "\${words[2]}" in
        list-clusters)
          _values 'option' --full
          ;;
        generate-completion-script)
          _values 'shell' bash zsh
          ;;
        ceph-df|ceph-cleanup|cluster-password|cluster-login|mtv-resources|run-tests|csi-nfs-df)
          _dev_tools_get_cluster_names
          ;;
      esac
      return
    fi

    if (( CURRENT == 4 )); then
      case "\${words[2]}" in
        ceph-df)
          _values 'option' --watch
          ;;
        ceph-cleanup)
          _values 'option' --execute
          ;;
      esac
      return
    fi
}

# Explicitly register the completion function
compdef _dev_tools ${script_name}
compdef _dev_tools ${script_path}
EOF
}

generate_completion_script() {
  local shell="${1-}"
  case "$shell" in
  bash)
    generate_bash_completion_script
    ;;
  zsh)
    generate_zsh_completion_script
    ;;
  *)
    echo -e "${C_RED}Error: unsupported shell '$shell'. Please use 'bash' or 'zsh'.${C_RESET}" >&2
    exit 1
    ;;
  esac
}

main() {
  if [[ "${1-}" == "--help" ]]; then
    usage
  fi

  local action="${1-}"
  if [ -z "$action" ]; then
    usage
  fi
  shift

  case "$action" in
  "cluster-password" | "cluster-login" | "mtv-resources" | "run-tests" | "ceph-cleanup" | "ceph-df" | "csi-nfs-df")
    if [ -z "${1-}" ]; then
      echo -e "${C_RED}Error: Missing cluster name for action '$action'.${C_RESET}" >&2
      usage
    fi
    ;;
  "list-clusters" | "generate-completion-script")
    # No cluster name needed
    ;;
  *)
    printf "${C_RED}Unsupported action: %s${C_RESET}\n" "$action" >&2
    usage
    ;;
  esac

  case "$action" in
  "cluster-password")
    cluster-password "$1"
    ;;
  "cluster-login")
    local cluster_info
    cluster_info=$(cluster-login "$1")
    print-cluster-data-tree "$cluster_info" "$1"
    ;;
  "mtv-resources")
    mtv-resources "$1"
    ;;
  "run-tests")
    run-tests "$@"
    ;;
  "ceph-cleanup")
    ceph-cleanup "$@"
    ;;
  "ceph-df")
    ceph-df "$@"
    ;;
  "list-clusters")
    list-clusters "$@"
    ;;
  "csi-nfs-df")
    cluster-login "$1" --no-copy >/dev/null
    "$SCRIPT_DIR/check_nfs_csi_space.sh"
    ;;
  "generate-completion-script")
    generate_completion_script "$@"
    ;;
  esac
}

main "$@"
