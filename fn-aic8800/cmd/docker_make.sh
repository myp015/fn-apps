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
Usage: $ME [--image IMAGE] [--pkgs PACKAGE(s)] [--strip] -- <make-args>

This script runs a kernel module build inside a Docker container to ensure a clean
build environment with the correct kernel headers.

Example:
  $ME -- make M=$(pwd) CONFIG_NTFS3_FS=m CONFIG_NTFS3_LZX_XPRESS=y modules

Flags:
  --image IMAGE       docker image to use (default: auto-detect based on host OS)
  --pkgs PACKAGE(s)   install additional package(s) inside the container 
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

CONTAINER_NAME=""
PULLED_IMAGE=false
cleanup_docker() {
  local rc="${1:-$?}"

  trap - EXIT

  if [ -n "${CONTAINER_NAME:-}" ] && docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
    echo "Cleaning up Docker container ${CONTAINER_NAME}..."
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || echo "WARN: Failed to remove container ${CONTAINER_NAME}" >&2
  fi

  if [ "${PULLED_IMAGE:-false}" = true ]; then
    echo "Removing pulled Docker image ${IMAGE}..."
    docker rmi "${IMAGE}" >/dev/null 2>&1 || echo "WARN: Failed to remove Docker image ${IMAGE}" >&2
  fi

  exit "${rc}"
}

BUILD=""
SPACE=""
IMAGE=""
STRIP=false
PKGS=() # support multiple packages

if [ "$#" -eq 0 ]; then
  usage
  exit 11
fi

docker --version >/dev/null 2>&1 || die 21 "ERROR: Docker is not installed."

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    --image)
      IMAGE="$2"
      shift 2
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

if [ -z "${IMAGE}" ]; then
  if [ -r /etc/os-release ]; then
    source /etc/os-release
    case "${ID,,}" in
      debian) IMAGE="debian:${VERSION_CODENAME:-bookworm}" ;;
      *) IMAGE="ubuntu:${VERSION_CODENAME:-22.04}" ;;
    esac
  else
    IMAGE="ubuntu:22.04"
  fi
fi

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
CONTAINER_NAME="docker_make_${$}_$(date +%s)"
trap cleanup_docker EXIT

echo "Docker image : ${IMAGE}"
echo "Install pkgs : ${PKGS[*]}"
echo "Linux headers: $(basename "${BUILD}")"
echo "Work space   : ${SPACE}"
echo "Make args    : ${MAKE_ARGS[*]}"

# Run the build inside a Docker container, mounting necessary directories
echo "Running build inside Docker..."

HAS_DOCKER_IMAGE=$(docker images -q "${IMAGE}" 2>/dev/null)
[ -n "${HAS_DOCKER_IMAGE}" ] || {
  echo "Docker image ${IMAGE} not found locally; pulling..."
  docker pull "${IMAGE}" 2>/dev/null || die "Failed to pull Docker image ${IMAGE}"
  PULLED_IMAGE=true
}

# Escape MAKE_ARGS for safe embedding in bash -lc
MAKE_ARGS_ESCAPED=""
for a in "${MAKE_ARGS[@]}"; do
  MAKE_ARGS_ESCAPED+=" $(printf '%q' "${a}")"
  [ "$HAS_C" = false ] && [ "${a}" = "make" ] && MAKE_ARGS_ESCAPED+=" $(printf '%q' "-C ${BUILD}")"
done

# Install required packages inside the container
PKGS_ESCAPED=""
for p in "${PKGS[@]}"; do
  PKGS_ESCAPED+=" $(printf '%q' "${p}")"
done

docker run --rm --name "${CONTAINER_NAME}" -v /usr:/usr.host:ro -v "${BUILD}":"${BUILD}":ro -v "${SPACE}":"${SPACE}":rw -w "${SPACE}" "${IMAGE}" bash -lc "
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
PATH=/usr.host/bin:/usr.host/sbin:\${PATH:-} LD_LIBRARY_PATH=/usr.host/lib/x86_64-linux-gnu:/usr.host/lib:\${LD_LIBRARY_PATH:-} ${MAKE_ARGS_ESCAPED} 2>&1 | tee build.log
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

cleanup_docker "${rc}"
