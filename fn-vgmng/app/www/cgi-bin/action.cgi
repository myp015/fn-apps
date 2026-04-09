#!/bin/bash

. "$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)/common.sh"

echo "Content-Type: application/json; charset=utf-8"
echo ""

request_body=""
if [ -n "${CONTENT_LENGTH:-}" ] && [ "${CONTENT_LENGTH}" -gt 0 ] 2>/dev/null; then
  request_body=$(dd bs=1 count="${CONTENT_LENGTH}" 2>/dev/null)
fi

url_decode() {
  local value="${1//+/ }"
  printf '%b' "${value//%/\\x}"
}

request_action=""
request_device=""
request_mode="ro"
request_target=""
request_auto="1"
effective_mode=""
state_device=""
mount_device=""

original_ifs=$IFS
IFS='&'
set -- ${request_body}
IFS=$original_ifs
for pair in "$@"; do
  field_name=${pair%%=*}
  field_value=${pair#*=}
  case "${field_name}" in
    action)
      request_action=$(url_decode "${field_value}")
      ;;
    device)
      request_device=$(url_decode "${field_value}")
      ;;
    mode)
      request_mode=$(url_decode "${field_value}")
      ;;
    target)
      request_target=$(url_decode "${field_value}")
      ;;
    auto)
      request_auto=$(url_decode "${field_value}")
      ;;
  esac
done

send_response() {
  local ok="$1"
  local message="$2"
  printf '{"ok":%s,"message":"%s","status":%s}\n' "${ok}" "$(escape_json "${message}")" "$(status_payload)"
  exit 0
}

send_error() {
  log_msg "ERROR: $*"
  send_response false "$*"
}

resolve_request_mount_device() {
  local requested_device="$1"
  local resolved_device=""

  state_device="${requested_device}"
  mount_device="${requested_device}"

  case "${requested_device}" in
    /dev/mapper/* | /dev/md*)
      return 0
      ;;
  esac

  if is_degraded_md_mount_candidate "${requested_device}"; then
    resolved_device=$(assemble_degraded_md_member "${requested_device}" || true)
    if [ -n "${resolved_device}" ]; then
      mount_device="${resolved_device}"
    fi
  fi
}

enforce_mount_mode() {
  local requested_mode="$1"
  local requested_device="$2"
  local resolved_device="$3"

  if [ "${requested_mode}" != "rw" ]; then
    printf 'ro'
    return 0
  fi

  if [ "$(device_mountmode "${resolved_device}")" = "ro" ] || [ "$(device_mountmode "${requested_device}")" = "ro" ]; then
    log_msg "force read-only mount for ${requested_device} via ${resolved_device}"
    printf 'ro'
    return 0
  fi

  printf 'rw'
}

activate_degraded_md_members() {
  local block_device="$1"
  local candidate
  local assembled_device

  while read -r candidate; do
    [ -b "${candidate}" ] || continue
    is_degraded_md_mount_candidate "${candidate}" || continue

    assembled_device=$(assemble_degraded_md_member "${candidate}" || true)
    if [ -n "${assembled_device}" ]; then
      log_msg "md member assembled ${candidate} -> ${assembled_device}"
      if command_exists pvscan; then
        pvscan --cache "${assembled_device}" >>"${LOG_FILE}" 2>&1 || true
      fi
    fi
  done < <(list_probe_paths "${block_device}")
}

device_parent_path() {
  local device_path="$1"
  local parent_name=""

  [ -n "${device_path}" ] || return 1
  [ -b "${device_path}" ] || return 1

  parent_name=$(lsblk -dn -o PKNAME "${device_path}" 2>/dev/null | head -n 1)
  [ -n "${parent_name}" ] || return 1

  case "${parent_name}" in
    /dev/*)
      printf '%s' "${parent_name}"
      ;;
    *)
      printf '/dev/%s' "${parent_name}"
      ;;
  esac
}

cleanup_import_device_stack() {
  local source_device="$1"
  local vg_name
  local lineage_paths=()
  local current_path=""
  local idx

  [ -n "${source_device}" ] || return 0
  [ -b "${source_device}" ] || return 0

  current_path="${source_device}"
  while [ -n "${current_path}" ] && [ -b "${current_path}" ]; do
    lineage_paths+=("${current_path}")
    current_path=$(device_parent_path "${current_path}" || true)
    case " ${lineage_paths[*]} " in
      *" ${current_path} "*)
        break
        ;;
    esac
  done

  [ "${#lineage_paths[@]}" -gt 0 ] || return 0

  while read -r vg_name; do
    [ -n "${vg_name}" ] || continue
    if vg_has_mounted_lvs "${vg_name}"; then
      log_msg "cleanup kept vg ${vg_name}: logical volumes still mounted"
      continue
    fi
    if command_exists vgchange; then
      log_msg "deactivate volume group ${vg_name}"
      vgchange -an "${vg_name}" >>"${LOG_FILE}" 2>&1 || true
    fi
  done < <(find_vgs_for_paths "${lineage_paths[@]}" | awk '!seen[$0]++')

  for ((idx = 0; idx < ${#lineage_paths[@]}; idx++)); do
    current_path="${lineage_paths[$idx]}"
    case "${current_path}" in
      /dev/mapper/*)
        if device_has_mounted_descendants "${current_path}"; then
          continue
        fi
        if device_has_children "${current_path}"; then
          continue
        fi
        if command_exists dmsetup; then
          log_msg "remove mapper device ${current_path}"
          dmsetup remove "$(basename "${current_path}")" >>"${LOG_FILE}" 2>&1 || true
        fi
        ;;
      /dev/md*)
        if device_has_mounted_descendants "${current_path}"; then
          continue
        fi
        if device_has_children "${current_path}"; then
          continue
        fi
        if command_exists mdadm; then
          log_msg "stop md device ${current_path}"
          mdadm --stop "${current_path}" >>"${LOG_FILE}" 2>&1 || true
        fi
        ;;
    esac
  done
}

scan_storage_stack() {
  if command_exists pvscan; then
    pvscan --cache >>"${LOG_FILE}" 2>&1 || pvscan >>"${LOG_FILE}" 2>&1 || true
  fi

  if command_exists vgscan; then
    vgscan --mknodes >>"${LOG_FILE}" 2>&1 || true
  fi

  if command_exists vgchange; then
    vgchange -ay >>"${LOG_FILE}" 2>&1 || true
  fi

  if command_exists btrfs; then
    btrfs device scan >>"${LOG_FILE}" 2>&1 || true
  fi
}

refresh_block_device_topology() {
  local block_device="$1"

  [ -n "${block_device}" ] || return 0
  [ -b "${block_device}" ] || return 0

  if command_exists blockdev; then
    blockdev --rereadpt "${block_device}" >>"${LOG_FILE}" 2>&1 || true
  fi

  if command_exists partprobe; then
    partprobe "${block_device}" >>"${LOG_FILE}" 2>&1 || true
  fi

  if command_exists partx; then
    partx -u "${block_device}" >>"${LOG_FILE}" 2>&1 || true
    partx -a "${block_device}" >>"${LOG_FILE}" 2>&1 || true
  fi

  if command_exists udevadm; then
    udevadm settle >>"${LOG_FILE}" 2>&1 || true
  fi
}

vg_has_mounted_lvs() {
  local vg_name="$1"
  local lv_path

  [ -n "${vg_name}" ] || return 1
  command_exists lvs || return 1

  while read -r lv_path; do
    lv_path=$(printf '%s' "${lv_path}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [ -n "${lv_path}" ] || continue
    if findmnt -rn -S "${lv_path}" >/dev/null 2>&1; then
      return 0
    fi
  done < <(lvs --noheadings -o lv_path "${vg_name}" 2>/dev/null)

  return 1
}

find_vgs_for_paths() {
  local tmp_file
  local path
  local pv_name
  local vg_name

  command_exists pvs || return 0
  [ "$#" -gt 0 ] || return 0

  tmp_file=$(mktemp "${APP_VAR_DIR}/paths.XXXXXX") || return 0
  for path in "$@"; do
    [ -n "${path}" ] || continue
    printf '%s\n' "${path}" >>"${tmp_file}"
  done

  while read -r pv_name vg_name; do
    pv_name=$(printf '%s' "${pv_name}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    vg_name=$(printf '%s' "${vg_name}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [ -n "${pv_name}" ] || continue
    [ -n "${vg_name}" ] || continue
    if grep -Fxq "${pv_name}" "${tmp_file}"; then
      printf '%s\n' "${vg_name}"
    fi
  done < <(pvs --noheadings -o pv_name,vg_name 2>/dev/null)

  rm -f "${tmp_file}"
}

unmount_managed_target() {
  local managed_target="$1"
  local preserve_auto_state="${2:-0}"
  local source_device=""

  [ -n "${managed_target}" ] || return 0

  is_managed_mount_target "${managed_target}" || return 1
  findmnt -rn "${managed_target}" >/dev/null 2>&1 || return 0

  source_device=$(findmnt -rn "${managed_target}" -o SOURCE 2>/dev/null | head -n 1)

  log_msg "unmount ${managed_target}"
  if ! umount "${managed_target}" >>"${LOG_FILE}" 2>&1; then
    log_msg "regular unmount failed for ${managed_target}, trying lazy unmount"
    sync >>"${LOG_FILE}" 2>&1 || true
    umount -l "${managed_target}" >>"${LOG_FILE}" 2>&1 || return 1
  fi

  unregister_app_share "${managed_target}" >>"${LOG_FILE}" 2>&1 || log_msg "app share cleanup skipped for ${managed_target}"
  if [ "${preserve_auto_state}" != "1" ]; then
    forget_auto_mount "" "${managed_target}" >>"${LOG_FILE}" 2>&1 || log_msg "auto-mount state cleanup skipped for ${managed_target}"
  fi
  if [ "${managed_target}" != "${MOUNT_ROOT}" ]; then
    rmdir "${managed_target}" >/dev/null 2>&1 || true
  fi

  if [ -n "${source_device}" ]; then
    cleanup_import_device_stack "${source_device}"
  fi

  return 0
}

shutdown_managed_mounts() {
  local managed_target=""
  local failed=0

  while read -r managed_target; do
    [ -n "${managed_target}" ] || continue
    if ! unmount_managed_target "${managed_target}" 1; then
      failed=1
      log_msg "shutdown unmount failed for ${managed_target}"
    fi
  done < <(list_managed_mount_targets | awk '{ print length($0) "|" $0 }' | sort -rn | cut -d'|' -f2-)

  prune_stale_app_shares

  [ "${failed}" -eq 0 ]
}

md_device_is_inactive() {
  local md_path="$1"
  local md_name=""

  [ -n "${md_path}" ] || return 1
  md_name=$(basename "${md_path}")
  [ -n "${md_name}" ] || return 1

  awk -v name="${md_name}" '
    $1 == name && $2 == ":" && $3 == "inactive" {
      found = 1
      exit
    }
    END { exit(found ? 0 : 1) }
  ' /proc/mdstat >/dev/null 2>&1
}

stop_inactive_parent_md_children() {
  local block_device="$1"
  local md_child

  command_exists mdadm || return 0
  [ -b "${block_device}" ] || return 0

  while read -r md_child; do
    [ -b "${md_child}" ] || continue
    md_device_is_inactive "${md_child}" || continue
    log_msg "stop inactive parent md ${md_child} for ${block_device}"
    mdadm --stop "${md_child}" >>"${LOG_FILE}" 2>&1 || true
  done < <(lsblk -pnr -o PATH,TYPE "${block_device}" 2>/dev/null | awk -v root="${block_device}" '
    $1 != root && ($2 == "md" || $2 ~ /^raid/) { print $1 }
  ' | awk '!seen[$0]++')
}

probe_partitions_missing() {
  local block_device="$1"
  local partition_count=0

  [ -b "${block_device}" ] || return 1

  while read -r _partition_path; do
    partition_count=$((partition_count + 1))
    break
  done < <(list_partition_nodes "${block_device}")

  [ "${partition_count}" -eq 0 ]
}

refresh_manual_probe_roots() {
  local block_device="$1"
  local candidate_root

  if [ -n "${block_device}" ] && [ -b "${block_device}" ]; then
    refresh_block_device_topology "${block_device}"
    stop_inactive_parent_md_children "${block_device}"
    if probe_partitions_missing "${block_device}"; then
      refresh_block_device_topology "${block_device}"
    fi
    return 0
  fi

  while read -r candidate_root; do
    [ -b "${candidate_root}" ] || continue
    refresh_block_device_topology "${candidate_root}"
    stop_inactive_parent_md_children "${candidate_root}"
    if probe_partitions_missing "${candidate_root}"; then
      refresh_block_device_topology "${candidate_root}"
    fi
  done < <(list_inactive_foreign_roots)
}

manually_assemble_all_arrays() {
  local block_device="$1"

  refresh_manual_probe_roots "${block_device}"
  activate_degraded_md_members "${block_device}"
}

activate_storage_stack() {
  local block_device="$1"

  manually_assemble_all_arrays "${block_device}"
  scan_storage_stack
}

probe_block_device() {
  local block_device="$1"

  [ -b "${block_device}" ] || send_error "Device not found: ${block_device}"

  log_msg "probe ${block_device}"

  refresh_block_device_topology "${block_device}"

  activate_storage_stack "${block_device}"
}

case "${request_action}" in
  activate)
    log_msg "activate requested"
    activate_storage_stack
    prune_stale_app_shares
    send_response true "Foreign arrays and volume groups activated"
    ;;
  probe)
    [ -n "${request_device}" ] || send_error "Missing device parameter"
    case "${request_device}" in
      /dev/*) ;;
      *)
        send_error "Only /dev block devices can be probed"
        ;;
    esac
    probe_block_device "${request_device}"
    send_response true "Device probed and arrays activated"
    ;;
  mount)
    [ -n "${request_device}" ] || send_error "Missing device parameter"
    case "${request_device}" in
      /dev/*) ;;
      *)
        send_error "Only /dev block devices can be mounted"
        ;;
    esac

    resolve_request_mount_device "${request_device}"
    [ -b "${mount_device}" ] || send_error "Device not found: ${request_device}"

    fstype=$(detect_fs_type "${mount_device}")
    [ -n "${fstype}" ] || send_error "Failed to detect filesystem type"

    is_supported_mount_type "${fstype}" || send_error "Unsupported filesystem: ${fstype}"

    request_mode=$(enforce_mount_mode "${request_mode}" "${state_device}" "${mount_device}")

    existing_target=$(detect_active_mountpoint "${mount_device}" || true)
    if [ -z "${existing_target}" ] && [ "${state_device}" != "${mount_device}" ]; then
      existing_target=$(detect_active_mountpoint "${state_device}" || true)
    fi

    if [ -z "${request_target}" ]; then
      if [ -n "${existing_target}" ] && is_managed_mount_target "${existing_target}"; then
        effective_mode=$(detect_mount_mode "${existing_target}" "${mount_device}")
        if [ "${request_auto}" = "0" ] || [ "${request_auto}" = "false" ]; then
          forget_auto_mount "${state_device}" "${existing_target}" >>"${LOG_FILE}" 2>&1 || log_msg "auto-mount state cleanup skipped for ${state_device}"
        else
          remember_auto_mount "${state_device}" "${existing_target}" "${effective_mode}" >>"${LOG_FILE}" 2>&1 || log_msg "auto-mount state update skipped for ${state_device}"
        fi
        send_response true "Mounted at $(mount_alias_path "${existing_target}")"
      fi
      request_target=$(next_mount_point "${state_device}" "${fstype}") || send_error "Failed to allocate import mount path"
    else
      is_managed_mount_target "${request_target}" || send_error "Mount target must be under ${APP_SHARE_ROOT}"
      request_target=$(resolve_mount_target "${request_target}") || send_error "Invalid mount target: ${request_target}"
    fi

    if [ -n "${existing_target}" ]; then
      if is_legacy_mount_target "${existing_target}"; then
        log_msg "migrate legacy mount ${existing_target} -> ${request_target}"
        if migrate_managed_mount "${existing_target}" "${request_target}"; then
          send_response true "Mounted at $(mount_alias_path "${request_target}")"
        fi

        log_msg "legacy move unsupported, remount ${existing_target} -> ${request_target}"
        if ! umount "${existing_target}" >>"${LOG_FILE}" 2>&1; then
          sync >>"${LOG_FILE}" 2>&1 || true
          umount -l "${existing_target}" >>"${LOG_FILE}" 2>&1 || send_error "Failed to release legacy mount: ${existing_target}"
        fi
        rmdir "${existing_target}" >/dev/null 2>&1 || true
        existing_target=""
      else
        send_error "Device already mounted at ${existing_target}"
      fi
    fi

    mkdir -p "${request_target}" || send_error "Failed to create mount directory ${request_target}"
    if findmnt -rn "${request_target}" >/dev/null 2>&1; then
      send_error "Mount point already in use: ${request_target}"
    fi

    log_msg "mount ${mount_device} (${state_device}) to ${request_target} as ${fstype} (${request_mode})"
    mount -t "$(normalize_mount_fstype "${fstype}")" -o "$(mount_options "${fstype}" "${request_mode}")" "${mount_device}" "${request_target}" >>"${LOG_FILE}" 2>&1 || send_error "Mount failed: ${request_device}"
    effective_mode=$(detect_mount_mode "${request_target}" "${mount_device}")
    register_app_share "${request_target}" >>"${LOG_FILE}" 2>&1 || log_msg "app share registration skipped for ${request_target}"
    if [ "${request_auto}" = "0" ] || [ "${request_auto}" = "false" ]; then
      forget_auto_mount "${state_device}" "${request_target}" >>"${LOG_FILE}" 2>&1 || log_msg "auto-mount state cleanup skipped for ${state_device}"
    else
      remember_auto_mount "${state_device}" "${request_target}" "${effective_mode}" >>"${LOG_FILE}" 2>&1 || log_msg "auto-mount state update skipped for ${state_device}"
    fi
    send_response true "Mounted at $(mount_alias_path "${request_target}")"
    ;;
  unmount)
    [ -n "${request_target}" ] || send_error "Missing mount target parameter"
    is_managed_mount_target "${request_target}" || send_error "Only imported mount path can be unmounted"
    request_target=$(resolve_mount_target "${request_target}") || send_error "Invalid mount target: ${request_target}"

    findmnt -rn "${request_target}" >/dev/null 2>&1 || send_error "Mount point is not active: ${request_target}"
    unmount_managed_target "${request_target}" 0 || send_error "Unmount failed: ${request_target}"
    send_response true "Unmounted $(mount_alias_path "${request_target}") and released idle import resources"
    ;;
  shutdown)
    log_msg "shutdown requested"
    shutdown_managed_mounts || send_error "Failed to fully unmount imported volumes"
    send_response true "Imported volumes unmounted and import resources released"
    ;;
  auto-mount)
    [ -n "${request_device}" ] || send_error "Missing device parameter"
    case "${request_device}" in
      /dev/*) ;;
      *)
        send_error "Only /dev block devices are supported"
        ;;
    esac

    resolve_request_mount_device "${request_device}"
    request_device="${state_device}"

    if [ -n "${request_target}" ]; then
      is_managed_mount_target "${request_target}" || send_error "Mount target must be under ${APP_SHARE_ROOT}"
      request_target=$(resolve_mount_target "${request_target}") || send_error "Invalid mount target: ${request_target}"
    else
      request_target=$(detect_active_mountpoint "${request_device}" || true)
      if [ -z "${request_target}" ] && [ "${mount_device}" != "${request_device}" ]; then
        request_target=$(detect_active_mountpoint "${mount_device}" || true)
      fi
    fi

    [ -n "${request_target}" ] || send_error "Device is not mounted: ${request_device}"
    is_managed_mount_target "${request_target}" || send_error "Only imported mount path can be configured"

    if [ "${request_auto}" = "0" ] || [ "${request_auto}" = "false" ]; then
      forget_auto_mount "${request_device}" "${request_target}" >>"${LOG_FILE}" 2>&1 || log_msg "auto-mount state cleanup skipped for ${request_device}"
      send_response true "Auto-mount disabled for $(mount_alias_path "${request_target}")"
    fi

    effective_mode=$(enforce_mount_mode "${request_mode}" "${request_device}" "${mount_device}")
    if [ "${effective_mode}" != "rw" ]; then
      effective_mode=$(detect_mount_mode "${request_target}" "${request_device}")
    fi
    remember_auto_mount "${request_device}" "${request_target}" "${effective_mode}" >>"${LOG_FILE}" 2>&1 || send_error "Failed to update auto-mount state"
    send_response true "Auto-mount enabled for $(mount_alias_path "${request_target}")"
    ;;
  cleanup)
    log_msg "cleanup requested"
    prune_stale_app_shares
    send_response true "Idle import resources released"
    ;;
  *)
    send_error "Unsupported action"
    ;;
esac
