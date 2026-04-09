#!/bin/bash

. "$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)/common.sh"

echo "Content-Type: application/json; charset=utf-8"
echo ""

emit_json_field() {
  local field_name="$1"
  shift

  printf '"%s":' "${field_name}"
  "$@"
}

printf '{'
printf '"timestamp":"%s",' "$(date '+%Y-%m-%d %H:%M:%S')"
printf '"mountRoot":"%s",' "${MOUNT_ROOT}"
printf '"mountAliasRoot":"%s",' "${APP_SHARE_ROOT}"
emit_json_field "lsblk" command_json lsblk -J -p -o PATH,NAME,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,PKNAME
printf ','
emit_json_field "inventory" disk_inventory_json
printf ','
emit_json_field "findmnt" mount_table_json
printf ','
emit_json_field "autoMounts" auto_mounts_json
printf ','
emit_json_field "vgs" command_json vgs --reportformat json --units b --nosuffix -o vg_name,vg_uuid,pv_count,lv_count
printf ','
emit_json_field "lvs" command_json lvs --reportformat json --units b --nosuffix -o lv_path,lv_name,vg_name,lv_size,lv_attr
printf ','
emit_json_field "pvs" command_json pvs --reportformat json --units b --nosuffix -o pv_name,vg_name,pv_uuid,pv_size,pv_attr
printf ','
printf '"mdstat":"%s",' "$(cat /proc/mdstat 2>/dev/null | escape_json)"
printf '"logTail":"%s"' "$(tail -n 120 "${LOG_FILE}" 2>/dev/null | escape_json)"
printf '}'
