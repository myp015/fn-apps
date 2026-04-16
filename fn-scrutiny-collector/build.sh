#!/usr/bin/env bash
#
# Scrutiny Collector FPK 构建脚本
# 参照 fn-terminal 的构建方式：platform=all 单包双架构、手动 tar 打包
#
# 用法:
#   ./build.sh          # 自动获取最新版本
#   ./build.sh 1.33.0   # 手动指定版本

set -e

WORKDIR="$(
  cd "$(dirname "$0")"
  pwd
)"

get_latest_version() {
  local tag
  tag=$(curl -fsSL -w "%{url_effective}" -o /dev/null "https://github.com/Starosdev/scrutiny/releases/latest" \
    | awk -F'/' '{print $NF}' | sed 's/^[v|V]//g')
  if [ -z "$tag" ]; then
    echo "ERROR: Failed to get latest version" >&2
    exit 1
  fi
  echo "$tag"
}

#SCRUTINY_VERSION="${1:-$(get_latest_version)}"
SCRUTINY_VERSION="1.49.2"
echo "Building Scrutiny Collector v${SCRUTINY_VERSION} ..."

ARCHS=(x86_64 aarch64)
declare -A COLLECTOR_ASSET
COLLECTOR_ASSET[x86_64]="scrutiny-collector-metrics-linux-amd64"
COLLECTOR_ASSET[aarch64]="scrutiny-collector-metrics-linux-arm64"

for arch in "${ARCHS[@]}"; do
  asset="${COLLECTOR_ASSET[$arch]}"
  url="https://github.com/Starosdev/scrutiny/releases/download/v${SCRUTINY_VERSION}/${asset}"
  cachefile="/tmp/${asset}-${SCRUTINY_VERSION}"

  echo "Downloading Collector for ${arch} ..."
  if [ -f "${cachefile}" ]; then
    echo "  Using cached: ${cachefile}"
  else
    curl -fsSL "${url}" -o "${cachefile}"
  fi

  mkdir -p "${WORKDIR}/app/bin/${arch}"
  cp "${cachefile}" "${WORKDIR}/app/bin/${arch}/scrutiny-collector-metrics"
  chmod +x "${WORKDIR}/app/bin/${arch}/scrutiny-collector-metrics"

  echo "  Done: app/bin/${arch}/scrutiny-collector-metrics"
done

# Update manifest version
if [[ "$OSTYPE" == "darwin"* ]]; then
  sed -i '' "s/^version[[:space:]]*=.*/version               = ${SCRUTINY_VERSION}/" "${WORKDIR}/manifest"
else
  sed -i "s/^version[[:space:]]*=.*/version               = ${SCRUTINY_VERSION}/" "${WORKDIR}/manifest"
fi

APPNAME=$(grep -w '^appname' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)
VERSION=$(grep -w '^version' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)
PLATFORM=$(grep -w '^platform' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)

rm -f "${WORKDIR}/app.tgz" "$(dirname "${WORKDIR}")/${APPNAME}_${PLATFORM}_v${VERSION}.fpk" 2>/dev/null || true
tar -czf "${WORKDIR}/app.tgz" -C "${WORKDIR}/app" . >/dev/null 2>&1
tar -czf "$(dirname "${WORKDIR}")/${APPNAME}_${PLATFORM}_v${VERSION}.fpk" \
  -C "${WORKDIR}" cmd config wizard app.tgz ICON.PNG ICON_256.PNG manifest >/dev/null 2>&1

rm -f "${WORKDIR}/app.tgz"

# Clean up downloaded binaries
for arch in "${ARCHS[@]}"; do
  rm -rf "${WORKDIR}/app/bin/${arch}/scrutiny-collector-metrics"
done

echo "Done: $(dirname "${WORKDIR}")/${APPNAME}_${PLATFORM}_v${VERSION}.fpk"

exit 0
