#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"

STEP="init"
cgi_install_trap

load_cfg
ensure_iface

# Prefer runtime hotspot iface (may be a virtual AP iface).
load_nat_state
HOTSPOT_DEV="${HOTSPOT_IFACE:-$IFACE}"

# If the hotspot device is not operating as AP (e.g. it's in STA/managed mode),
# do not show clients — STA-mode interfaces do not have associated stations.
if command -v iw >/dev/null 2>&1; then
  if ! iw dev "$HOTSPOT_DEV" info 2>/dev/null | grep -q "type AP"; then
    http_ok_begin
    json_begin_named_array "clients"
    json_end
    http_ok_end
    exit 0
  fi
fi

TAB="$(printf '\t')"

ipv4_in_cidr() {
  ip="${1:-}"
  cidr="${2:-}"
  [ -n "${ip:-}" ] || return 1
  [ -n "${cidr:-}" ] || return 1

  awk -v ip="$ip" -v cidr="$cidr" '
    function ip2int(s, a, n, i, v) {
      n = split(s, a, ".")
      if (n != 4) {
        return -1
      }
      v = 0
      for (i = 1; i <= 4; i++) {
        if (a[i] !~ /^[0-9]+$/ || a[i] < 0 || a[i] > 255) {
          return -1
        }
        v = (v * 256) + a[i]
      }
      return v
    }
    BEGIN {
      n = split(cidr, parts, "/")
      if (n != 2) {
        exit 1
      }
      addr = ip2int(ip)
      base = ip2int(parts[1])
      prefix = parts[2]
      if (addr < 0 || base < 0) {
        exit 1
      }
      if (prefix !~ /^[0-9]+$/ || prefix < 0 || prefix > 32) {
        exit 1
      }
      if (prefix == 0) {
        exit 0
      }
      block = 2 ^ (32 - prefix)
      exit int(addr / block) == int(base / block) ? 0 : 1
    }
  ' </dev/null
}

filter_ip_for_hotspot() {
  ip="${1:-}"
  [ -n "${ip:-}" ] || return 1
  if ipv4_in_cidr "$ip" "$IP_CIDR"; then
    printf '%s' "$ip"
    return 0
  fi
  return 1
}

stations=""
if command -v iw >/dev/null 2>&1; then
  stations="$(iw dev "$HOTSPOT_DEV" station dump 2>/dev/null | awk '
    BEGIN{mac=""; sig=""; ct=""; rx=""; tx=""}
    /^Station /{
      if (mac!="") {print mac"\t"sig"\t"ct"\t"rx"\t"tx}
      mac=$2; sig=""; ct=""; rx=""; tx="";
      next
    }
    $1=="signal:"{sig=$2; next}
    $1=="connected" && $2=="time:"{ct=$3; next}
    $1=="rx" && $2=="bytes:"{rx=$3; next}
    $1=="tx" && $2=="bytes:"{tx=$3; next}
    END{ if (mac!="") {print mac"\t"sig"\t"ct"\t"rx"\t"tx} }
  ' || true)"
fi

# Neighbor table: MAC -> IP (best-effort)
neigh=""
if command -v ip >/dev/null 2>&1; then
  neigh_raw="$(ip neigh show dev "$HOTSPOT_DEV" 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="lladdr"){print tolower($(i+1))"\t"$1}}}' || true)"
  if [ -n "${neigh_raw:-}" ]; then
    while IFS="$TAB" read -r mac ipaddr; do
      [ -n "${mac:-}" ] || continue
      ipaddr="$(filter_ip_for_hotspot "${ipaddr:-}" || true)"
      [ -n "${ipaddr:-}" ] || continue
      neigh="${neigh}${mac}${TAB}${ipaddr}
"
    done <<EOF
$neigh_raw
EOF
  fi
fi

# Best-effort hostname/IP mapping from DHCP leases.
lease_hosts_mac=""
lease_hosts_ip=""
lease_ips_mac=""
if command -v awk >/dev/null 2>&1; then
  leases_raw=""
  for f in /var/lib/NetworkManager/dnsmasq-*.leases /var/lib/misc/dnsmasq.leases /tmp/dnsmasq.leases; do
    [ -r "$f" ] || continue
    leases_raw="$leases_raw$(cat "$f" 2>/dev/null || true)"
  done

  if [ -n "${leases_raw:-}" ]; then
    lease_hosts_mac="$(printf '%s' "$leases_raw" | awk '
      NF>=4 {
        mac=tolower($2); host=$4;
        if (host=="" || host=="*" || host=="-") next;
        print mac"\t"host;
      }
    ' 2>/dev/null || true)"
    leases_filtered="$(printf '%s' "$leases_raw" | awk '
      NF>=4 {
        mac=tolower($2); ip=$3; host=$4;
        if (ip=="") next;
        print mac"\t"ip"\t"host;
      }
    ' 2>/dev/null || true)"
    if [ -n "${leases_filtered:-}" ]; then
      while IFS="$TAB" read -r mac ipaddr host; do
        [ -n "${mac:-}" ] || continue
        ipaddr="$(filter_ip_for_hotspot "${ipaddr:-}" || true)"
        [ -n "${ipaddr:-}" ] || continue
        lease_ips_mac="${lease_ips_mac}${mac}${TAB}${ipaddr}
"
        if [ -n "${host:-}" ] && [ "$host" != "*" ] && [ "$host" != "-" ]; then
          lease_hosts_ip="${lease_hosts_ip}${ipaddr}${TAB}${host}
"
        fi
      done <<EOF
$leases_filtered
EOF
    fi
  fi
fi

http_ok_begin
json_begin_named_array "clients"
seen=" "

emit_client() {
  mac="$1"
  ipaddr="$2"
  sig="$3"
  ct="$4"
  rx_bytes="$5"
  tx_bytes="$6"
  [ -n "$mac" ] || return 0

  case "$seen" in
    *" $mac "*) return 0 ;;
  esac
  seen="$seen$mac "

  json_begin_object
  json_kv_string "mac" "$mac"
  hostname=""
  if [ -n "${lease_hosts_mac:-}" ]; then
    hostname="$(printf '%s\n' "$lease_hosts_mac" | awk -v m="$mac" 'tolower($1)==tolower(m){print $2; exit}' || true)"
  fi
  if [ -z "${hostname:-}" ] && [ -n "${ipaddr:-}" ] && [ -n "${lease_hosts_ip:-}" ]; then
    hostname="$(printf '%s\n' "$lease_hosts_ip" | awk -v ip="$ipaddr" '$1==ip{print $2; exit}' || true)"
  fi
  if [ -z "${hostname:-}" ] && [ -n "${ipaddr:-}" ] && command -v getent >/dev/null 2>&1; then
    hostname="$(getent hosts "$ipaddr" 2>/dev/null | awk '{print $2; exit}' || true)"
  fi
  if [ -n "${hostname:-}" ]; then
    json_kv_string "hostname" "$hostname"
  fi
  if [ -n "${ipaddr:-}" ]; then
    json_kv_string "ip" "$ipaddr"
  fi
  if [ -n "${sig:-}" ]; then
    json_kv_raw "signalDbm" "$(json_escape "$sig")"
  fi
  if [ -n "${ct:-}" ]; then
    json_kv_raw "connectedSeconds" "$(json_escape "$ct")"
  fi
  if [ -n "${rx_bytes:-}" ]; then
    json_kv_raw "rxBytes" "$(json_escape "$rx_bytes")"
  fi
  if [ -n "${tx_bytes:-}" ]; then
    json_kv_raw "txBytes" "$(json_escape "$tx_bytes")"
  fi
  json_end
}

# Prefer station dump first (includes signal/time). Add IP if present in neigh.
if [ -n "${stations:-}" ]; then
  while IFS="$TAB" read -r mac sig ct rx tx; do
    [ -n "${mac:-}" ] || continue
    ipaddr=""
    if [ -n "${lease_ips_mac:-}" ]; then
      ipaddr="$(printf '%s\n' "$lease_ips_mac" | awk -v m="$mac" 'tolower($1)==tolower(m){print $2; exit}' || true)"
    fi
    if [ -z "${ipaddr:-}" ] && [ -n "${neigh:-}" ]; then
      ipaddr="$(printf '%s\n' "$neigh" | awk -v m="$mac" 'tolower($1)==tolower(m){print $2; exit}' || true)"
    fi
    emit_client "$mac" "$ipaddr" "$sig" "$ct" "$rx" "$tx"
  done <<EOF
$stations
EOF
fi

# Then add neighbor-derived entries not in station list.
# Only use neighbor table as fallback when we have no station data.
# Neighbor entries can linger after a client disconnects (STALE), causing ghost clients.
if [ -z "${stations:-}" ] && [ -n "${neigh:-}" ]; then
  while IFS="$TAB" read -r mac ipaddr; do
    [ -n "${mac:-}" ] || continue
    emit_client "$mac" "$ipaddr" "" "" "" ""
  done <<EOF
$neigh
EOF
fi
json_end
http_ok_end
