#!/bin/bash
set -euo pipefail

USAGE="Usage: $(basename "$0") enable <bm-host-ip>
       $(basename "$0") disable

Configure DNS resolution for bare-metal host via resolvectl.

Commands:
  enable <ip>  Set the BM host IP as DNS server for the detected interface
  disable      Revert DNS settings on the interface that has mtv.local configured

Examples:
  $(basename "$0") enable 10.46.248.80
  $(basename "$0") disable"

die() {
    echo "Error: $1" >&2
    exit 1
}

get_interface() {
    local ip="$1"
    local route_output
    route_output="$(ip route get "$ip" 2>/dev/null)" || die "Cannot determine route to $ip"

    local iface
    iface="$(echo "$route_output" | grep -oP 'dev \K\S+')" || die "Cannot parse interface from route output"

    [[ -n "$iface" ]] || die "No interface found for $ip"
    echo "$iface"
}

get_mtv_interface() {
    local current_iface=""
    local found_iface=""

    local link_re='^Link [0-9]+ \(([^)]+)\)'
    while IFS= read -r line; do
        if [[ "$line" =~ $link_re ]]; then
            current_iface="${BASH_REMATCH[1]}"
        elif [[ -n "$current_iface" && "$line" =~ DNS\ Domain:.*mtv\.local ]]; then
            found_iface="$current_iface"
            break
        fi
    done < <(resolvectl status 2>/dev/null)

    [[ -n "$found_iface" ]] || die "No interface found with mtv.local DNS domain configured"
    echo "$found_iface"
}

[[ $# -ge 1 ]] || { echo "$USAGE" >&2; exit 1; }

ACTION="$1"

[[ "$ACTION" == "enable" || "$ACTION" == "disable" ]] || die "Invalid action '$ACTION'. Must be 'enable' or 'disable'."

case "$ACTION" in
    enable)
        [[ $# -eq 2 ]] || { echo "$USAGE" >&2; exit 1; }
        IP="$2"
        IFACE="$(get_interface "$IP")"
        echo "Detected interface: $IFACE"
        echo "Setting DNS server $IP on $IFACE"
        sudo resolvectl dns "$IFACE" "$IP"
        sudo resolvectl domain "$IFACE" '~mtv.local'
        echo "DNS setup enabled for $IP on $IFACE"
        ;;
    disable)
        [[ $# -eq 1 ]] || { echo "$USAGE" >&2; exit 1; }
        IFACE="$(get_mtv_interface)"
        echo "Detected interface: $IFACE"
        echo "Removing mtv.local DNS domain from $IFACE"
        sudo resolvectl domain "$IFACE" ""
        echo "Removing BM DNS server from $IFACE"
        sudo resolvectl dns "$IFACE" ""
        echo "DNS setup disabled on $IFACE"
        ;;
esac
