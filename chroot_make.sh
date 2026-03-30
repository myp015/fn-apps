#!/usr/bin/env bash
#
# Copyright (C) 2022 Ing <https://github.com/wjz304>
#
# This is free software, licensed under the MIT License.
# See /LICENSE for more information.
#

ME=$(basename "$0")
usage() {
  cat <<EOF
Usage: $ME [--pkgs PACKAGE(s)] [--strip] -- <make-args>

This script runs a kernel module build inside a chroot environment to ensure a clean
build environment with the correct kernel headers.

Example:
  $ME -- make M=$(pwd) CONFIG_NTFS3_FS=m CONFIG_NTFS3_LZX_XPRESS=y modules

Flags:
  --pkgs PACKAGE(s)   install additional package(s) inside the chroot environment
                      (accept comma-separated or space-separated list; default: build-essential)
  --strip             strip the module after build (default: false)
  -h, --help          show this help
EOF
}

die() {
  local rc=1

  if [ "$#" -gt 0 ] && [[ $1 =~ ^[0-9]+$ ]]; then
    rc="$1"
    shift
  fi

  [ "$#" -gt 0 ] && echo "ERROR: $*" >&2
  exit "${rc}"
}

abs_overlayfs=""
space_mount_target=""
CHROOT_ENV_CREATED_BY_SCRIPT=false
MERGED_MOUNTED_BY_SCRIPT=false
DEV_MOUNTED_BY_SCRIPT=false
DEVPTS_MOUNTED_BY_SCRIPT=false
PROC_MOUNTED_BY_SCRIPT=false
SYS_MOUNTED_BY_SCRIPT=false
RUN_RESOLVCONF_MOUNTED_BY_SCRIPT=false
RUN_UDEV_MOUNTED_BY_SCRIPT=false
SPACE_MOUNTED_BY_SCRIPT=false
cleanup_chroot() {
  local rc=$?

  trap - EXIT

  [ -n "${abs_overlayfs:-}" ] || exit "${rc}"

  echo "Cleaning up chroot environment..."

  if [ "${SPACE_MOUNTED_BY_SCRIPT}" = true ] && [ -n "${space_mount_target:-}" ] && mountpoint -q "${space_mount_target}"; then
    umount "${space_mount_target}" 2>/dev/null || umount -l "${space_mount_target}" 2>/dev/null \
      || echo "WARN: Failed to unmount ${space_mount_target}" >&2
  fi

  [ "${RUN_UDEV_MOUNTED_BY_SCRIPT}" = true ] && mountpoint -q "${abs_overlayfs}/merged/run/udev" \
    && (umount "${abs_overlayfs}/merged/run/udev" 2>/dev/null || umount -l "${abs_overlayfs}/merged/run/udev" 2>/dev/null \
      || echo "WARN: Failed to unmount ${abs_overlayfs}/merged/run/udev" >&2)
  [ "${RUN_RESOLVCONF_MOUNTED_BY_SCRIPT}" = true ] && mountpoint -q "${abs_overlayfs}/merged/run/resolvconf" \
    && (umount "${abs_overlayfs}/merged/run/resolvconf" 2>/dev/null || umount -l "${abs_overlayfs}/merged/run/resolvconf" 2>/dev/null \
      || echo "WARN: Failed to unmount ${abs_overlayfs}/merged/run/resolvconf" >&2)
  [ "${SYS_MOUNTED_BY_SCRIPT}" = true ] && mountpoint -q "${abs_overlayfs}/merged/sys" \
    && (umount "${abs_overlayfs}/merged/sys" 2>/dev/null || umount -l "${abs_overlayfs}/merged/sys" 2>/dev/null \
      || echo "WARN: Failed to unmount ${abs_overlayfs}/merged/sys" >&2)
  [ "${PROC_MOUNTED_BY_SCRIPT}" = true ] && mountpoint -q "${abs_overlayfs}/merged/proc" \
    && (umount "${abs_overlayfs}/merged/proc" 2>/dev/null || umount -l "${abs_overlayfs}/merged/proc" 2>/dev/null \
      || echo "WARN: Failed to unmount ${abs_overlayfs}/merged/proc" >&2)
  [ "${DEVPTS_MOUNTED_BY_SCRIPT}" = true ] && mountpoint -q "${abs_overlayfs}/merged/dev/pts" \
    && (umount "${abs_overlayfs}/merged/dev/pts" 2>/dev/null || umount -l "${abs_overlayfs}/merged/dev/pts" 2>/dev/null \
      || echo "WARN: Failed to unmount ${abs_overlayfs}/merged/dev/pts" >&2)
  [ "${DEV_MOUNTED_BY_SCRIPT}" = true ] && mountpoint -q "${abs_overlayfs}/merged/dev" \
    && (umount "${abs_overlayfs}/merged/dev" 2>/dev/null || umount -l "${abs_overlayfs}/merged/dev" 2>/dev/null \
      || echo "WARN: Failed to unmount ${abs_overlayfs}/merged/dev" >&2)
  [ "${MERGED_MOUNTED_BY_SCRIPT}" = true ] && mountpoint -q "${abs_overlayfs}/merged" \
    && (umount "${abs_overlayfs}/merged" 2>/dev/null || umount -l "${abs_overlayfs}/merged" 2>/dev/null \
      || echo "WARN: Failed to unmount ${abs_overlayfs}/merged" >&2)

  [ "${CHROOT_ENV_CREATED_BY_SCRIPT}" = true ] \
    && rm -rf "${abs_overlayfs}/merged" "${abs_overlayfs}/upper" "${abs_overlayfs}/work" 2>/dev/null || true

  exit "${rc}"
}

chroot_env_exists() {
  [ -d "${abs_overlayfs}/upper" ] && [ -d "${abs_overlayfs}/work" ] && [ -d "${abs_overlayfs}/merged" ]
}

chroot_env_is_mounted() {
  mountpoint -q "${abs_overlayfs}/merged"
}

BUILD=""
SPACE=""
STRIP=false
PKGS=() # support multiple packages

if [ "$#" -eq 0 ]; then
  usage
  exit 11
fi

command -v chroot >/dev/null 2>&1 || die 21 "ERROR: chroot is not installed."

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --pkgs)
      # accept comma-separated or space-separated package lists, and allow repeating --pkgs
      shift
      # collect tokens until next option (starting with --) or the -- separator
      while [ "$#" -gt 0 ] && [ "${1}" != "--" ] && [[ ${1} != --* ]]; do
        # split comma-separated entries in each token
        IFS=',' read -ra _pkgs <<<"${1}"
        for _p in "${_pkgs[@]}"; do
          _p_trim=$(echo "${_p}" | xargs)
          [ -n "${_p_trim}" ] && PKGS+=("${_p_trim}")
        done
        shift
      done
      ;;
    --strip)
      STRIP=true
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 12
      ;;
  esac
done

MAKE_ARGS=()
HAS_C=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    -C?*)
      HAS_C=true
      echo "$1" | grep -q '^-C' && BUILD="$(echo "$1" | sed -E 's/^-C=?//')"
      MAKE_ARGS+=("$1")
      shift
      ;;
    -C)
      HAS_C=true
      BUILD="$2"
      MAKE_ARGS+=("$1" "$2")
      shift 2
      ;;
    M=*)
      SPACE="$(echo "$1" | cut -d= -f2-)"
      MAKE_ARGS+=("$1")
      shift
      ;;
    *)
      MAKE_ARGS+=("$1")
      shift
      ;;
  esac
done

if [ -z "${BUILD}" ]; then
  KVER="$(uname -r)"
  if [ -L "/lib/modules/${KVER}/build" ]; then
    BUILD="$(readlink -f "/lib/modules/${KVER}/build" || true)"
  elif [ -d "/usr/src/linux-headers-${KVER}" ]; then
    BUILD="/usr/src/linux-headers-${KVER}"
  fi
fi
[ ! -f "${BUILD}/Makefile" ] && die 13 "ERROR: Please specify a valid kernel build directory with -C option."
BUILD="$(realpath -m -- "${BUILD}")"

SPACE="${SPACE:-$(pwd)}"
[ ! -f "${SPACE}/Makefile" ] && die 14 "ERROR: Please specify a valid workspace directory with M= option."
SPACE="$(realpath -m -- "${SPACE}")"

[ ${#PKGS[@]} -eq 0 ] && PKGS+=(build-essential)

echo "Install pkgs : ${PKGS[*]}"
echo "Linux headers: $(basename "${BUILD}")"
echo "Work space   : ${SPACE}"
echo "Make args    : ${MAKE_ARGS[*]}"
echo "Running build inside chroot..."

resolve_overlayfs() {
  local abs_overlayfs
  abs_overlayfs="$(realpath -m -- "${TRIM_OVERLAYFS:-}" 2>/dev/null)"
  [ -n "${abs_overlayfs:-}" ] || die 10 "TRIM_OVERLAYFS environment variable is not set."
  [ "${abs_overlayfs:-}" = "/" ] && die 10 "Refusing to use / as overlayfs path."
  printf '%s\n' "${abs_overlayfs:-}"
}

TRIM_OVERLAYFS="${TRIM_OVERLAYFS:-/opt/trim_overlayfs}"
abs_overlayfs="$(resolve_overlayfs)"
trap cleanup_chroot EXIT

if chroot_env_exists; then
  echo "Found existing chroot environment: ${abs_overlayfs}"
else
  mkdir -p "${abs_overlayfs:-}"/{lower,upper,work,merged} || die 15 "ERROR: Failed to create overlayfs directories under ${TRIM_OVERLAYFS}"
  CHROOT_ENV_CREATED_BY_SCRIPT=true
  echo "Initialized chroot environment: ${abs_overlayfs}"
fi

if chroot_env_is_mounted; then
  echo "Reusing mounted chroot environment: ${abs_overlayfs}/merged"
else
  mount -t overlay overlay -o "rw,relatime,lowerdir=/,upperdir=${abs_overlayfs:-}/upper,workdir=${abs_overlayfs:-}/work" "${abs_overlayfs:-}/merged" \
    || die 16 "ERROR: Mounting OverlayFS failed: ${abs_overlayfs:-}/merged"
  MERGED_MOUNTED_BY_SCRIPT=true
fi

mountpoint -q "${abs_overlayfs:-}/merged/dev" || {
  mount --bind /dev "${abs_overlayfs:-}/merged/dev" || die "Mounting /dev failed: ${abs_overlayfs:-}/merged/dev"
  DEV_MOUNTED_BY_SCRIPT=true
}
mountpoint -q "${abs_overlayfs:-}/merged/dev/pts" || {
  mount --bind /dev/pts "${abs_overlayfs:-}/merged/dev/pts" || die "Mounting /dev/pts failed: ${abs_overlayfs:-}/merged/dev/pts"
  DEVPTS_MOUNTED_BY_SCRIPT=true
}
mountpoint -q "${abs_overlayfs:-}/merged/proc" || {
  mount --bind /proc "${abs_overlayfs:-}/merged/proc" || die "Mounting /proc failed: ${abs_overlayfs:-}/merged/proc"
  PROC_MOUNTED_BY_SCRIPT=true
}
mountpoint -q "${abs_overlayfs:-}/merged/sys" || {
  mount --bind /sys "${abs_overlayfs:-}/merged/sys" || die "Mounting /sys failed: ${abs_overlayfs:-}/merged/sys"
  SYS_MOUNTED_BY_SCRIPT=true
}

mkdir -p "${abs_overlayfs:-}"/merged/run/{resolvconf,udev} #|| die "Creating runtime directories failed: ${abs_overlayfs:-}/merged/run"
[ -d /run/resolvconf ] && ! mountpoint -q "${abs_overlayfs:-}/merged/run/resolvconf" && {
  mount --bind /run/resolvconf "${abs_overlayfs:-}/merged/run/resolvconf" || die "Mounting /run/resolvconf failed: ${abs_overlayfs:-}/merged/run/resolvconf"
  RUN_RESOLVCONF_MOUNTED_BY_SCRIPT=true
}
[ -d /run/udev ] && ! mountpoint -q "${abs_overlayfs:-}/merged/run/udev" && {
  mount --bind /run/udev "${abs_overlayfs:-}/merged/run/udev" || die "Mounting /run/udev failed: ${abs_overlayfs:-}/merged/run/udev"
  RUN_UDEV_MOUNTED_BY_SCRIPT=true
}

space_mount_target="${abs_overlayfs:-}/merged${SPACE}"
mkdir -p "${space_mount_target}" || die "Creating workspace mount point failed: ${space_mount_target}"
mountpoint -q "${space_mount_target}" || {
  mount --bind "${SPACE}" "${space_mount_target}" || die "Mounting workspace failed: ${SPACE} -> ${space_mount_target}"
  SPACE_MOUNTED_BY_SCRIPT=true
}

MAKE_ARGS_ESCAPED=""
for a in "${MAKE_ARGS[@]}"; do
  MAKE_ARGS_ESCAPED+=" $(printf '%q' "${a}")"
  [ "$HAS_C" = false ] && [ "${a}" = "make" ] && MAKE_ARGS_ESCAPED+=" $(printf '%q' "-C ${BUILD}")"
done

# Install required packages inside the chroot environment
PKGS_ESCAPED=""
for p in "${PKGS[@]}"; do
  PKGS_ESCAPED+=" $(printf '%q' "${p}")"
done

chroot "${abs_overlayfs}/merged" bash -lc "
#!/bin/bash
set -euo pipefail
[ -r /etc/os-release ] && . /etc/os-release || true
# Use Chinese mirrors (Tsinghua) for faster apt in China
case "\${ID:-}" in
  ubuntu)
    CODENAME="\${VERSION_CODENAME:-22.04}"
    cat >/etc/apt/sources.list <<EOF
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu/ \${CODENAME} main restricted universe multiverse
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu/ \${CODENAME}-updates main restricted universe multiverse
deb http://mirrors.tuna.tsinghua.edu.cn/ubuntu/ \${CODENAME}-security main restricted universe multiverse
EOF
    ;;
  debian)
    CODENAME="\${VERSION_CODENAME:-bookworm}"
    cat >/etc/apt/sources.list <<EOF
deb http://mirrors.tuna.tsinghua.edu.cn/debian/ \${CODENAME} main contrib non-free non-free-firmware
deb http://mirrors.tuna.tsinghua.edu.cn/debian/ \${CODENAME}-updates main contrib non-free non-free-firmware
deb http://mirrors.tuna.tsinghua.edu.cn/debian-security \${CODENAME}-security main contrib non-free non-free-firmware
EOF
    ;;
  *) ;;
esac
apt-get update -y >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends apt-utils ${PKGS_ESCAPED} >/dev/null
cd "${SPACE}"
${MAKE_ARGS_ESCAPED} 2>&1 | tee build.log
[ ${STRIP:-false} = true ] && find \"${SPACE}\" -name '*.ko' -exec strip -g {} + || true
"
rc=$?

# Fix ownership of generated files so they aren't owned by root
HOST_UID=$(id -u)
HOST_GID=$(id -g)
if [ "${HOST_UID}" -ne 0 ]; then
  echo "Fixing file ownership to ${HOST_UID}:${HOST_GID} in project directory..."
  sudo chown -R ${HOST_UID}:${HOST_GID} "${SPACE}" || true
fi

[ "${rc}" -eq 0 ] || {
  rc=3${rc}
  echo "Build failed with exit code ${rc}" >&2
}

cleanup_chroot ${rc}
