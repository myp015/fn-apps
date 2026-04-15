#!/bin/sh
# shellcheck disable=SC2034
set -eu
. "$(dirname "$0")/common.sh"

STEP="stpre"
cgi_install_trap

load_cfg
STEP="validate"
if ! validate_cfg; then
  err_msg="$(localize_msg "${CFG_ERR:-invalid config}")"
  http_ok_json "\"abort\":true, \"error\":\"$(json_escape "$err_msg")\""
  exit 0
fi

warnings_Num=0
warnings_list=""

# Ensure a Wi‑Fi iface is available / valid
STEP="iface-check"
if ! require_wifi_iface; then
  case $? in
    1)
      list="$(wifi_ifaces | tr '\n' ' ' | sed 's/ *$//')"
      err_msg="Device '${IFACE:-}' is not a Wi-Fi device. Wi-Fi devices: ${list}"
      err_msg="$(localize_msg "$err_msg")"
      http_ok_json "\"abort\":true, \"error\":\"$(json_escape "$err_msg")\""
      exit 0
      ;;
    2)
      # no wifi device
      err_msg="No Wi-Fi device found. Check 'nmcli dev status'."
      err_msg="$(localize_msg "$err_msg")"
      http_ok_json "\"abort\":true, \"error\":\"$(json_escape "$err_msg")\""
      exit 0
      ;;
  esac
fi

# Current STA connection (best-effort)
STEP="sta-check"
sta_prev_con=""
if command -v nmcli >/dev/null 2>&1; then
  sta_prev_con="$(nmcli -g GENERAL.CONNECTION dev show "${IFACE:-}" 2>/dev/null | head -n1 || true)"
  case "$sta_prev_con" in "" | "--") sta_prev_con="" ;; esac
fi

# Regulatory domain check
regdom="$(iw_reg_country 2>/dev/null || true)"
if [ "00" = "${regdom:-00}" ]; then
  warnings_list="${warnings_list}Warning: Country Code is (00); 5.0GHz channels may not be enabled.\n"
  warnings_Num=$((warnings_Num + 1))
fi

# STA+AP concurrent support check (non-destructive)
if ! iw_supports_sta_ap; then
  if [ -n "${sta_prev_con:-}" ]; then
    warnings_list="${warnings_list}Warning: Adapter does not support STA+AP; disconnected '${sta_prev_con}' on '${IFACE:-}'.\n"
    warnings_Num=$((warnings_Num + 1))
  else
    warnings_list="${warnings_list}Warning: Adapter does not support STA+AP; hotspot will use '${IFACE:-}' (may interrupt Wi‑Fi).\n"
    warnings_Num=$((warnings_Num + 1))
  fi
fi

# Uplink conflicts
STEP="uplink-check"
if [ -n "${UPLINK_IFACE:-}" ] && [ "${UPLINK_IFACE}" = "${IFACE:-}" ]; then
  err_msg="uplinkIface cannot be the same as hotspot iface (${IFACE:-}). Choose another uplink interface or leave uplinkIface empty (auto)."
  err_msg="$(localize_msg "$err_msg")"
  http_ok_json "\"abort\":true, \"error\":\"$(json_escape "$err_msg")\""
  exit 0
fi

# Check AP/hotspot mode support (best-effort)
STEP="ap-mode-check"
if command -v iw >/dev/null 2>&1; then
  if ! iw list 2>/dev/null | sed -n '/Supported interface modes:/,/^[[:space:]]*$/p' | grep -Eq "^[[:space:]]*\*[[:space:]]+AP\b"; then
    err_msg="Device '${IFACE:-}' does not appear to support AP/hotspot mode (iw list has no '* AP'). Use another Wi-Fi adapter."
    err_msg="$(localize_msg "$err_msg")"
    http_ok_json "\"abort\":true, \"error\":\"$(json_escape "$err_msg")\""
    exit 0
  fi
fi

# Runtime channel validation (best-effort warning)
STEP="channel-check"
if ! validate_runtime_channel; then
  warnmsg="${CFG_ERR:-channel invalid}"
  warnmsg="$(localize_msg "$warnmsg")"
  warnings_list="${warnings_list}${warnmsg}\n"
  warnings_Num=$((warnings_Num + 1))
fi

# Driver-level low-TX-power warning (best-effort)
STEP="txpower-check"
if warnmsg="$(wifi_low_power_notice "${IFACE:-}" 2>/dev/null || true)" && [ -n "${warnmsg:-}" ]; then
  warnmsg="$(localize_msg "$warnmsg")"
  warnings_list="${warnings_list}${warnmsg}\n"
  warnings_Num=$((warnings_Num + 1))
fi

# Build JSON success response
body=""
if [ "$warnings_Num" -gt 0 ]; then
  # Emit warnings array and default confirm texts
  http_json
  printf '{ "ok": true, "warnings": ['
  first=1
  echo "${warnings_list}" | sed '/^$/d' | while IFS= read -r line; do
    if [ "$first" -eq 1 ]; then
      first=0
    else
      printf ', '
    fi
      printf '"%s"' "$(json_escape "$line")"
  done
  printf '] }\n'
  exit 0
fi

# No warnings / no abort: return trivial ok JSON
http_ok_json ""
