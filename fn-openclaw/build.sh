#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
OPENCLAW_VERSION="2026.4.15"
NODE_VERSION="v22.22.0"

HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
  x86_64) HOST_NODE_ARCH="x64" ;;
  aarch64) HOST_NODE_ARCH="arm64" ;;
  *) echo "unsupported host arch: ${HOST_ARCH}"; exit 1 ;;
esac

HOST_NODE_DIR="${WORKDIR}/.build-host-node"
prepare_host_node() {
  rm -rf "${HOST_NODE_DIR}"
  mkdir -p "${HOST_NODE_DIR}"
  curl -fL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${HOST_NODE_ARCH}.tar.xz" -o "${HOST_NODE_DIR}/node.tar.xz"
  tar -xJf "${HOST_NODE_DIR}/node.tar.xz" -C "${HOST_NODE_DIR}" --strip-components=1
  rm -f "${HOST_NODE_DIR}/node.tar.xz"
}

build_arch() {
  local arch="$1"
  local node_arch=""
  case "${arch}" in
    x86_64) node_arch="x64" ;;
    aarch64) node_arch="arm64" ;;
    *) echo "unsupported arch: ${arch}"; return 1 ;;
  esac

  local out="${WORKDIR}/app/server/${arch}"
  local node_dir="${out}/node-runtime"
  local bundle_dir="${out}/openclaw-bundle"
  local tmp="${WORKDIR}/.build-${arch}"

  rm -rf "${out}" "${tmp}"
  mkdir -p "${out}" "${tmp}" "${node_dir}" "${bundle_dir}"

  echo "[fn-openclaw] building runtime for ${arch} ..."

  # Target node runtime (do not execute on non-matching host arch)
  curl -fL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${node_arch}.tar.xz" -o "${tmp}/node.tar.xz"
  tar -xJf "${tmp}/node.tar.xz" -C "${node_dir}" --strip-components=1

  # Build JS bundle with host node runtime.
  export PATH="${HOST_NODE_DIR}/bin:${PATH}"
  (
    cd "${tmp}"
    npm pack "openclaw@${OPENCLAW_VERSION}" --silent >/dev/null
    tar -xzf "openclaw-${OPENCLAW_VERSION}.tgz" -C "${bundle_dir}" --strip-components=1
    cd "${bundle_dir}"
    npm install --omit=dev --legacy-peer-deps --no-audit --no-fund \
      @larksuiteoapi/feishu-openclaw-plugin@2026.3.8 \
      @soimy/dingtalk@3.5.3 \
      @sunnoy/wecom@3.0.0 \
      @tencent-connect/openclaw-qqbot@1.7.1 >/dev/null
    node scripts/postinstall-bundled-plugins.mjs >/dev/null
  )

  rm -rf "${tmp}"
}

prepare_host_node
build_arch x86_64
build_arch aarch64

APPNAME=$(grep -w '^appname' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)
VERSION=$(grep -w '^version' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)
PLATFORM=$(grep -w '^platform' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)

rm -f "${WORKDIR}/app.tgz" "$(dirname "${WORKDIR}")/${APPNAME}_${PLATFORM}_v${VERSION}.fpk" 2>/dev/null || true

tar -czf "${WORKDIR}/app.tgz" -C "${WORKDIR}/app" .
tar -czf "$(dirname "${WORKDIR}")/${APPNAME}_${PLATFORM}_v${VERSION}.fpk" \
  -C "${WORKDIR}" cmd config wizard app.tgz ICON.PNG ICON_256.PNG manifest

rm -f "${WORKDIR}/app.tgz"
rm -rf "${HOST_NODE_DIR}"

echo "Done: ${APPNAME}_${PLATFORM}_v${VERSION}.fpk"
