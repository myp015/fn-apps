#!/bin/sh
# shellcheck disable=SC2034
set -eu
. "$(dirname "$0")/common.sh"

STEP="init"
cgi_install_trap

body="$(read_body)"

load_cfg
IFACE="$(form_get iface "$body")"
UPLINK_IFACE="$(form_get uplinkIface "$body")"
IP_CIDR="$(form_get ipCidr "$body")"
ALLOW_PORTS="$(form_get allowPorts "$body")"
SSID="$(form_get ssid "$body")"
PASSWORD="$(form_get password "$body")"
COUNTRY="$(form_get countryCode "$body")"
BAND="$(form_get band "$body")"
CHANNEL="$(form_get channel "$body")"
CHANNEL_WIDTH="$(form_get channelWidth "$body")"


# Option B: persist a concrete iface even if client submits empty.
ensure_iface
IFACE="$(normalize_parent_wifi_iface "${IFACE:-}")"

validate_cfg || http_err "400 Bad Request" "${CFG_ERR:-invalid config}"

save_cfg || http_err "500 Internal Server Error" "save config failed (CFG_FILE not writable)"
http_ok
