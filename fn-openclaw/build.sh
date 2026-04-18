#!/usr/bin/env bash
set -e

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
APPNAME=$(grep -w '^appname' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)
VERSION=$(grep -w '^version' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)
PLATFORM=$(grep -w '^platform' "${WORKDIR}/manifest" | awk -F= '{print $2}' | xargs)

rm -f "${WORKDIR}/app.tgz" "$(dirname "${WORKDIR}")/${APPNAME}_${PLATFORM}_v${VERSION}.fpk" 2>/dev/null || true

tar -czf "${WORKDIR}/app.tgz" -C "${WORKDIR}/app" .
tar -czf "$(dirname "${WORKDIR}")/${APPNAME}_${PLATFORM}_v${VERSION}.fpk" \
  -C "${WORKDIR}" cmd config wizard app.tgz ICON.PNG ICON_256.PNG manifest

rm -f "${WORKDIR}/app.tgz"
echo "Done: ${APPNAME}_${PLATFORM}_v${VERSION}.fpk"
