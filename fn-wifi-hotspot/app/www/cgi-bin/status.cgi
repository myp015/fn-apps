#!/bin/sh
# shellcheck disable=SC2034
set -eu
. "$(dirname "$0")/common.sh"

STEP="init"
cgi_install_trap

load_cfg
ensure_iface

# Keep status minimal: running, iface, hotspotIface, state, activeConnection, ip, uplinkIface
load_nat_state
parent_iface="$IFACE"
hotspot_iface="${HOTSPOT_IFACE:-$IFACE}"

dev_line="$(nmcli -t -f DEVICE,STATE,CONNECTION dev status 2>/dev/null | grep "^${hotspot_iface}:" || true)"
state="unknown"
active=""
if [ -n "$dev_line" ]; then
  state="$(printf '%s' "$dev_line" | cut -d: -f2)"
  active="$(printf '%s' "$dev_line" | cut -d: -f3-)"
fi

running="false"
[ "$active" = "$SSID" ] && running="true"

# Determine whether STA+AP concurrent mode is available (via iw)
sta_ap_concurrent="false"
if iw_supports_sta_ap; then
  sta_ap_concurrent="true"
fi

# Detect whether there is a parent (STA) active connection that would be disconnected
parent_active_connection=""
if command -v nmcli >/dev/null 2>&1; then
  parent_active_connection="$(nmcli -g GENERAL.CONNECTION dev show "$parent_iface" 2>/dev/null | head -n1 || true)"
  case "$parent_active_connection" in "" | "--") parent_active_connection="" ;; esac
fi

# If hotspot will reuse the parent iface and STA+AP is not supported, starting hotspot
# will disconnect the current STA connection.
will_disconnect_sta="false"
if [ "$hotspot_iface" = "$parent_iface" ] && [ "$sta_ap_concurrent" != "true" ] && [ -n "$parent_active_connection" ]; then
  will_disconnect_sta="true"
fi

ip="$(ip -4 addr show dev "$hotspot_iface" 2>/dev/null | awk '/inet[[:space:]]/{print $2; exit}' || true)"
txpower_dbm="$(wifi_txpower_dbm "$hotspot_iface" 2>/dev/null || true)"
wifi_driver="$(wifi_driver_name "$hotspot_iface" 2>/dev/null || true)"
low_txpower="false"
if wifi_txpower_is_suspiciously_low "$hotspot_iface"; then
  low_txpower="true"
fi
effective_uplink_iface="${NAT_UPLINK_IFACE:-${UPLINK_IFACE:-}}"
if [ -z "${effective_uplink_iface:-}" ]; then
  effective_uplink_iface="$(detect_route_dev 1.1.1.1 || true)"
fi

# Check internet connectivity via uplink iface (best-effort)
internet_status="false"
internet_reason="null"
internet_target="http://1.1.1.1"

if command -v curl >/dev/null 2>&1; then
  # if curl --interface "$hotspot_iface" --max-time 3 -I "$internet_target" --silent --output /dev/null; then
  if curl --max-time 3 -I "$internet_target" --silent --output /dev/null; then
    internet_status="true"
    internet_reason="null"
  else
    internet_status="false"
    internet_reason="curl failed on dev $hotspot_iface"
  fi
fi

http_ok_begin
json_begin_named_object "status"
json_kv_bool "running" "$running"
json_kv_string "iface" "$parent_iface"
json_kv_string "hotspotIface" "$hotspot_iface"
json_kv_string "state" "$state"
json_kv_string "activeConnection" "$active"
json_kv_string "parentActiveConnection" "${parent_active_connection:-}"
json_kv_bool "staApConcurrent" "$sta_ap_concurrent"
json_kv_bool "willDisconnectSta" "$will_disconnect_sta"
json_kv_string "ip" "${ip:-}"
json_kv_string "txPowerDbm" "${txpower_dbm:-}"
json_kv_string "wifiDriver" "${wifi_driver:-}"
json_kv_bool "lowTxPower" "$low_txpower"
json_kv_string "uplinkIface" "${UPLINK_IFACE:-}"
json_kv_string "effectiveUplinkIface" "${effective_uplink_iface:-}"
json_kv_raw "internetStatus" "$internet_status"
json_kv_string "internetReason" "${internet_reason:-}"
json_end
http_ok_end
