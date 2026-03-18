#!/bin/bash
set -euo pipefail

DOMAIN="mtv.local"
DOMAIN_EXPLICIT=false

_state_dir="${XDG_RUNTIME_DIR:-}"
if [[ -z "$_state_dir" || ! -d "$_state_dir" || ! -w "$_state_dir" ]]; then
  case "$(uname -s)" in
  Linux) _state_dir="/run/user/$(id -u)" ;;
  Darwin) _state_dir="$HOME/.cache" ;;
  *) _state_dir="$HOME/.cache" ;;
  esac
fi
# Final fallback to user-owned directory (never /tmp)
if [[ ! -d "$_state_dir" || ! -w "$_state_dir" ]]; then
  _state_dir="$HOME/.cache"
fi
STATE_FILE="${_state_dir}/bm-dns-setup.state"
unset _state_dir

USAGE="Usage: $(basename "$0") enable <bm-host-ip> [--domain <domain>]
       $(basename "$0") disable [--domain <domain>]
       $(basename "$0") status

Configure DNS resolution for bare-metal host.
Auto-detects OS (Linux uses resolvectl, macOS uses /etc/resolver).

Options:
  --domain <domain>  DNS domain to configure (default: $DOMAIN)

Commands:
  enable <ip>  Set the BM host IP as DNS server for the domain
  disable      Revert DNS settings (restores previous config)
               Use --domain <domain> if state file is missing
  status       Show current bm-dns-setup state

Examples:
  $(basename "$0") enable 10.46.248.80
  $(basename "$0") enable 10.46.248.80 --domain custom.local
  $(basename "$0") disable
  $(basename "$0") disable --domain mtv.local
  $(basename "$0") status"

die() {
  echo "Error: $1" >&2
  exit 1
}

validate_ip() {
  local ip="$1"
  local octet="(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
  [[ "$ip" =~ ^${octet}\.${octet}\.${octet}\.${octet}$ ]] || die "Invalid IP address: $ip"
}

validate_domain() {
  local domain="$1"
  [[ "$domain" != *..* && "$domain" != *. ]] || die "Invalid domain: $domain"

  local label
  local -a labels
  IFS='.' read -r -a labels <<<"$domain"
  for label in "${labels[@]}"; do
    [[ ${#label} -le 63 ]] || die "Invalid domain: $domain"
    [[ "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || die "Invalid domain: $domain"
  done
}

# --- Linux helpers (resolvectl) ---

get_interface() {
  local ip="$1"
  local route_output
  route_output="$(ip route get "$ip" 2>/dev/null)" || die "Cannot determine route to $ip"

  local iface
  iface="$(echo "$route_output" | grep -oP 'dev \K\S+')" || die "Cannot parse interface from route output"

  [[ -n "$iface" ]] || die "No interface found for $ip"
  echo "$iface"
}

save_state() {
  local iface="$1"
  local orig_dns="${2:-}"
  local orig_domains="${3:-}"
  local orig_resolver="${4:-}"
  mkdir -p "$(dirname "$STATE_FILE")"
  printf 'IFACE=%q\nORIG_DNS=%q\nORIG_DOMAINS=%q\nORIG_RESOLVER=%q\n' \
    "$iface" "$orig_dns" "$orig_domains" "$orig_resolver" >"$STATE_FILE"
  chmod 600 "$STATE_FILE"
  echo "Saved original config to $STATE_FILE"
}

load_state() {
  [[ -f "$STATE_FILE" ]] || die "No state file found at $STATE_FILE. Is bm-dns-setup active?"
  # shellcheck source=/dev/null
  source "$STATE_FILE"
}

get_current_dns() {
  local iface="$1"
  resolvectl dns "$iface" 2>/dev/null | sed "s/^Link [0-9]* ([^)]*): *//" || true
}

get_current_domains() {
  local iface="$1"
  resolvectl domain "$iface" 2>/dev/null | sed "s/^Link [0-9]* ([^)]*): *//" || true
}

enable_linux() {
  local ip="$1"
  local domain="$2"
  [[ ! -f "$STATE_FILE" ]] || die "bm-dns-setup is already active. Run 'disable' first or 'status' to check."
  local iface
  iface="$(get_interface "$ip")"
  echo "Detected interface: $iface"

  local orig_dns
  local orig_domains
  orig_dns="$(get_current_dns "$iface")"
  orig_domains="$(get_current_domains "$iface")"
  save_state "$iface" "$orig_dns" "$orig_domains"

  echo "Setting DNS server $ip on $iface for ~$domain"
  sudo resolvectl dns "$iface" "$ip"
  sudo resolvectl domain "$iface" "~$domain"
  echo "DNS setup enabled for $ip on $iface"
}

disable_linux() {
  local fallback_domain="${1:-}"
  if [[ ! -f "$STATE_FILE" ]]; then
    if [[ -n "$fallback_domain" ]]; then
      die "No state file found at $STATE_FILE. Cannot restore original DNS config without saved state."
    else
      die "No state file found at $STATE_FILE. Is bm-dns-setup active?"
    fi
  fi
  load_state
  local iface="$IFACE"
  local orig_dns="$ORIG_DNS"
  local orig_domains="$ORIG_DOMAINS"
  echo "Detected interface: $iface"
  echo "Restoring original DNS domains on $iface: ${orig_domains:-<none>}"
  if [[ -n "$orig_domains" ]]; then
    # shellcheck disable=SC2086
    sudo resolvectl domain "$iface" $orig_domains
  else
    sudo resolvectl domain "$iface" ""
  fi
  echo "Restoring original DNS servers on $iface: ${orig_dns:-<none>}"
  if [[ -n "$orig_dns" ]]; then
    # shellcheck disable=SC2086
    sudo resolvectl dns "$iface" $orig_dns
  else
    sudo resolvectl dns "$iface" ""
  fi
  rm -f "$STATE_FILE"
  echo "DNS setup disabled on $iface (original config restored)"
}

# --- macOS helpers (/etc/resolver) ---

enable_macos() {
  local ip="$1"
  local domain="$2"
  [[ ! -f "$STATE_FILE" ]] || die "bm-dns-setup is already active. Run 'disable' first or 'status' to check."
  local orig_resolver=""
  if [[ -f "/etc/resolver/$domain" ]]; then
    orig_resolver="$(cat "/etc/resolver/$domain")"
  fi
  # On macOS, save domain as IFACE (no interface needed; resolver files are per-domain)
  save_state "$domain" "" "" "$orig_resolver"
  echo "Setting up DNS resolver for $domain -> $ip"
  sudo mkdir -p /etc/resolver
  printf 'nameserver %s\n' "$ip" | sudo tee "/etc/resolver/$domain" >/dev/null
  echo "DNS setup enabled. Verifying..."
  sleep 1 # Allow macOS resolver cache to refresh
  scutil --dns | grep -F -A5 "$domain" || echo "Resolver added (may take a moment to activate)"
}

disable_macos() {
  local fallback_domain="${1:-}"
  if [[ -f "$STATE_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$STATE_FILE"
    local domain="$IFACE"
    local orig_resolver="${ORIG_RESOLVER:-}"
    if [[ -n "$orig_resolver" ]]; then
      echo "Restoring original $domain DNS resolver"
      printf '%s\n' "$orig_resolver" | sudo tee "/etc/resolver/$domain" >/dev/null
      echo "DNS setup disabled (original resolver restored)"
    elif [[ -f "/etc/resolver/$domain" ]]; then
      echo "Removing $domain DNS resolver"
      sudo rm "/etc/resolver/$domain"
      echo "DNS setup disabled"
    else
      echo "No $domain resolver found"
    fi
    rm -f "$STATE_FILE"
  elif [[ -n "$fallback_domain" ]]; then
    echo "Warning: No state file found. Using --domain '$fallback_domain' as fallback." >&2
    if [[ -f "/etc/resolver/$fallback_domain" ]]; then
      echo "Removing $fallback_domain DNS resolver"
      sudo rm "/etc/resolver/$fallback_domain"
      echo "DNS setup disabled"
    else
      die "No resolver found at /etc/resolver/$fallback_domain"
    fi
  else
    die "No state file found at $STATE_FILE. Use --domain <domain> to manually specify the domain to disable."
  fi
}

# --- Status helpers ---

status_linux() {
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "bm-dns-setup is not active (no state file found)"
    return
  fi
  # shellcheck source=/dev/null
  source "$STATE_FILE"
  echo "bm-dns-setup is active"
  echo "  Interface: $IFACE"
  echo "  Current DNS servers: $(get_current_dns "$IFACE")"
  echo "  Current DNS domains: $(get_current_domains "$IFACE")"
  echo "  Saved original DNS servers: ${ORIG_DNS:-<none>}"
  echo "  Saved original DNS domains: ${ORIG_DOMAINS:-<none>}"
}

status_macos() {
  if [[ ! -f "$STATE_FILE" ]]; then
    echo "bm-dns-setup is not active (no state file found)"
    return
  fi
  # shellcheck source=/dev/null
  source "$STATE_FILE"
  local domain="$IFACE"
  echo "bm-dns-setup is active"
  echo "  Domain: $domain"
  if [[ -f "/etc/resolver/$domain" ]]; then
    echo "  Resolver file: /etc/resolver/$domain"
    echo "  Contents:"
    sed 's/^/    /' "/etc/resolver/$domain"
  fi
}

# --- OS dispatch ---

run_enable() {
  local ip="$1"
  local domain="$2"
  case "$(uname -s)" in
  Linux) enable_linux "$ip" "$domain" ;;
  Darwin) enable_macos "$ip" "$domain" ;;
  *) die "Unsupported OS: $(uname -s). Supported: Linux, macOS." ;;
  esac
}

run_disable() {
  local domain="${1:-}"
  case "$(uname -s)" in
  Linux) disable_linux "$domain" ;;
  Darwin) disable_macos "$domain" ;;
  *) die "Unsupported OS: $(uname -s). Supported: Linux, macOS." ;;
  esac
}

run_status() {
  case "$(uname -s)" in
  Linux) status_linux ;;
  Darwin) status_macos ;;
  *) die "Unsupported OS: $(uname -s). Supported: Linux, macOS." ;;
  esac
}

# --- Main ---

parse_options() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
    --domain)
      [[ -n "${2:-}" ]] || die "--domain requires a value"
      DOMAIN="$2"
      DOMAIN_EXPLICIT=true
      shift 2
      ;;
    *) die "Unknown option: $1" ;;
    esac
  done
}

[[ $# -ge 1 ]] || {
  echo "$USAGE" >&2
  exit 1
}

ACTION="$1"
shift

[[ "$ACTION" == "enable" || "$ACTION" == "disable" || "$ACTION" == "status" ]] || die "Invalid action '$ACTION'. Must be 'enable', 'disable', or 'status'."

case "$ACTION" in
enable)
  [[ $# -ge 1 ]] || {
    echo "$USAGE" >&2
    exit 1
  }
  IP="$1"
  shift
  parse_options "$@"
  validate_ip "$IP"
  validate_domain "$DOMAIN"
  run_enable "$IP" "$DOMAIN"
  ;;
disable)
  parse_options "$@"
  if [[ "$DOMAIN_EXPLICIT" == "true" ]]; then
    validate_domain "$DOMAIN"
    run_disable "$DOMAIN"
  else
    run_disable
  fi
  ;;
status)
  run_status
  ;;
esac
