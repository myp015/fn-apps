#!/bin/sh
# shellcheck disable=SC2034
set -eu
. "$(dirname "$0")/common.sh"

STEP="init"
cgi_install_trap

load_cfg
STEP="validate"
validate_cfg || http_err "400 Bad Request" "${CFG_ERR:-invalid config}"

# Apply country/regulatory domain if specified.
if [ -n "${COUNTRY:-}" ]; then
  apply_regdom "$COUNTRY" || true
fi

# Re-check channel availability after applying regdom so we fail early with
# a clear message instead of bubbling up a vague nmcli error later.
validate_runtime_channel || http_err "400 Bad Request" "${CFG_ERR:-invalid channel}"

# Best-effort cleanup of old allow-port rules (in case previous stop didn't run)
remove_allow_ports

# Best-effort: ensure uplink device is connected when explicitly selected.
if [ -n "${UPLINK_IFACE:-}" ]; then
  nmcli dev connect "$UPLINK_IFACE" >/dev/null 2>&1 || true
fi

if ! require_wifi_iface; then
  list="$(wifi_ifaces | tr '\n' ' ' | sed 's/ *$//')"
  if [ -z "$list" ]; then
    http_err "400 Bad Request" "No Wi-Fi device found. Check 'nmcli dev status'."
  else
    http_err "400 Bad Request" "Device '${IFACE:-}' is not a Wi-Fi device. Wi-Fi devices: $list"
  fi
fi

# Decide which iface actually runs the hotspot.
# Default: reuse IFACE (will interrupt any STA connection).
parent_iface="$IFACE"
hotspot_iface="$IFACE"
virtual_iface=""

sta_prev_con=""
if command -v nmcli >/dev/null 2>&1; then
  sta_prev_con="$(nmcli -g GENERAL.CONNECTION dev show "$IFACE" 2>/dev/null | head -n1 || true)"
  case "$sta_prev_con" in "" | "--") sta_prev_con="" ;; esac
fi

if [ -n "${sta_prev_con:-}" ] && iw_supports_sta_ap; then
  virtual_iface="$(mk_ap_iface_name "$IFACE")"
  if ensure_virtual_ap_iface "$IFACE" "$virtual_iface"; then
    hotspot_iface="$virtual_iface"
  else
    virtual_iface=""
  fi
fi

# Do not use the same interface as both hotspot and uplink.
if [ -n "${UPLINK_IFACE:-}" ] && [ "$UPLINK_IFACE" = "$hotspot_iface" ]; then
  http_err "400 Bad Request" "uplinkIface cannot be the same as hotspot iface ($hotspot_iface). Choose another uplink interface or leave uplinkIface empty (auto)."
fi
if [ -n "${UPLINK_IFACE:-}" ] && [ "$UPLINK_IFACE" = "$IFACE" ] && [ "$hotspot_iface" = "$IFACE" ]; then
  http_err "400 Bad Request" "uplinkIface cannot be the same as hotspot iface ($IFACE) unless STA+AP concurrent mode is available."
fi

# Best-effort capability check: many Wi-Fi adapters cannot do AP/hotspot mode.
if command -v iw >/dev/null 2>&1; then
  if ! iw list 2>/dev/null | sed -n '/Supported interface modes:/,/^[[:space:]]*$/p' | grep -Eq '^[[:space:]]*\*[[:space:]]+AP\b'; then
    http_err "400 Bad Request" "Device '$IFACE' does not appear to support AP/hotspot mode (iw list has no '* AP'). Use another Wi-Fi adapter."
  fi
fi

if [ -n "${sta_prev_con:-}" ]; then
  nmcli con down id "$sta_prev_con" >/dev/null 2>&1 || true
fi

out=""
nmcli_wait="${NMCLI_WAIT_SECS:-20}"
# Create the hotspot profile with the final settings first, then activate once.
# This avoids the previous start flow where NetworkManager brought up a default
# hotspot first and we immediately bounced it again to apply the desired IP.
nmcli con down id "$SSID" >/dev/null 2>&1 || true
nmcli con delete "$SSID" >/dev/null 2>&1 || true
nmcli device disconnect "$hotspot_iface" >/dev/null 2>&1 || true
if ! out="$(nmcli con add type wifi ifname "$hotspot_iface" con-name "$SSID" autoconnect no ssid "$SSID" 2>&1)"; then
  rc=$?
  nmcli con down id "$SSID" >/dev/null 2>&1 || true
  nmcli con delete "$SSID" >/dev/null 2>&1 || true
  nmcli device disconnect "$hotspot_iface" >/dev/null 2>&1 || true
  if [ -n "${sta_prev_con:-}" ]; then
    nmcli con up id "$sta_prev_con" >/dev/null 2>&1 || true
  fi
  out="$(sanitize_text "${out:-}" || true)"
  http_err "500 Internal Server Error" "$out"
fi

if [ -n "${IP_CIDR:-}" ]; then
  nmcli_mod_cmd() {
    nmcli con mod "$SSID" \
      802-11-wireless.mode ap \
      802-11-wireless.band "$BAND" \
      802-11-wireless.channel "$CHANNEL" \
      802-11-wireless.powersave 2 \
      802-11-wireless-security.key-mgmt wpa-psk \
      802-11-wireless-security.psk "$PASSWORD" \
      802-11-wireless-security.proto rsn \
      802-11-wireless-security.pairwise ccmp \
      ipv4.method shared \
      ipv4.addresses "$IP_CIDR"
  }
else
  nmcli_mod_cmd() {
    nmcli con mod "$SSID" \
      802-11-wireless.mode ap \
      802-11-wireless.band "$BAND" \
      802-11-wireless.channel "$CHANNEL" \
      802-11-wireless.powersave 2 \
      802-11-wireless-security.key-mgmt wpa-psk \
      802-11-wireless-security.psk "$PASSWORD" \
      802-11-wireless-security.proto rsn \
      802-11-wireless-security.pairwise ccmp \
      ipv4.method shared
  }
fi

if ! nmcli_mod_cmd 2>/tmp/fn-hotspot-nmcli-mod.err; then
  nmcli_err="$(sanitize_text "$(cat /tmp/fn-hotspot-nmcli-mod.err 2>/dev/null || true)")"
  rm -f /tmp/fn-hotspot-nmcli-mod.err >/dev/null 2>&1 || true
  nmcli con down id "$SSID" >/dev/null 2>&1 || true
  nmcli con delete "$SSID" >/dev/null 2>&1 || true
  nmcli device disconnect "$hotspot_iface" >/dev/null 2>&1 || true
  if [ -n "${sta_prev_con:-}" ]; then
    nmcli con up id "$sta_prev_con" >/dev/null 2>&1 || true
  fi
  http_err "500 Internal Server Error" "${nmcli_err:-nmcli: failed to configure hotspot connection '$SSID'}"
fi
rm -f /tmp/fn-hotspot-nmcli-mod.err >/dev/null 2>&1 || true

# Try to apply requested channel width (best-effort). NetworkManager keys vary by
# version/driver; attempt common settings before the first activation so we do
# not need to bounce an already-running hotspot.
if [ -n "${CHANNEL_WIDTH:-}" ]; then
  case "${CHANNEL_WIDTH}" in
    20)
      nmcli con mod "$SSID" 802-11-wireless.ht-mode "" >/dev/null 2>&1 || true
      nmcli con mod "$SSID" 802-11-wireless.vht-mode "" >/dev/null 2>&1 || true
      ;;
    40)
      # Prefer HT40+; try both just in case
      nmcli con mod "$SSID" 802-11-wireless.ht-mode HT40+ >/dev/null 2>&1 || nmcli con mod "$SSID" 802-11-wireless.ht-mode HT40- >/dev/null 2>&1 || true
      nmcli con mod "$SSID" 802-11-wireless.vht-mode "" >/dev/null 2>&1 || true
      ;;
    80)
      nmcli con mod "$SSID" 802-11-wireless.vht-mode VHT80 >/dev/null 2>&1 || true
      nmcli con mod "$SSID" 802-11-wireless.ht-mode "" >/dev/null 2>&1 || true
      ;;
    160)
      nmcli con mod "$SSID" 802-11-wireless.vht-mode VHT160 >/dev/null 2>&1 || true
      nmcli con mod "$SSID" 802-11-wireless.ht-mode "" >/dev/null 2>&1 || true
      ;;
    *)
      # unknown: ignore
      ;;
  esac
fi
# Activate once after all desired settings are in place.
if ! nmcli_out="$(nmcli --wait "$nmcli_wait" con up id "$SSID" 2>&1)"; then
  rc=$?
  nmcli_err="$(sanitize_text "${nmcli_out:-}" || true)"
  nmcli con down id "$SSID" >/dev/null 2>&1 || true
  nmcli con delete "$SSID" >/dev/null 2>&1 || true
  nmcli device disconnect "$hotspot_iface" >/dev/null 2>&1 || true
  if [ -n "${sta_prev_con:-}" ]; then
    nmcli con up id "$sta_prev_con" >/dev/null 2>&1 || true
  fi
  if [ "$rc" -eq 8 ] 2>/dev/null; then
    http_err "504 Gateway Timeout" "Hotspot setup timed out after ${nmcli_wait}s.
$nmcli_err"
  fi
  http_err "500 Internal Server Error" "nmcli: failed to bring up hotspot connection '$SSID'
$nmcli_err"
fi

# Best-effort: ensure hotspot clients can reach internet.
# Some environments don't set up NAT automatically.
apply_hotspot_nat "$hotspot_iface" "${UPLINK_IFACE:-}" "$parent_iface" "$virtual_iface"

# Best-effort: allow hotspot clients to access host services on selected ports.
apply_allow_ports "$hotspot_iface" "${ALLOW_PORTS:-}"

# Persist enabled state so we can restore after reboot.
write_hotspot_state 1

notice=""
notice="$(wifi_low_power_notice "$hotspot_iface" 2>/dev/null || true)"
http_ok_output "$out" "${notice:-}"
