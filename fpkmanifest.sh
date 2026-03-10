#!/usr/bin/env bash
#
# Copyright (C) 2022 Ing <https://github.com/wjz304>
#
# This is free software, licensed under the MIT License.
# See /LICENSE for more information.
#

APP="${1:-}"
KEY="${2:-}"
VAL="${3:-}"

if [ -z "${APP}" ] || [ -z "${KEY}" ] || [ -z "${VAL}" ]; then
  echo "Usage: $0 <app> <key> <val>"
  exit 1
fi

if [ ! -f "${APP}" ] || [[ ${APP} != *.fpk ]]; then
  echo "ERROR: File ${APP} does not exist or is not a fpk file"
  exit 1
fi

APP_PATH=$(dirname "${APP}")/$(basename "${APP}" .fpk)

mkdir -p "${APP_PATH}"
tar -xzf "${APP}" -C "${APP_PATH}"
if grep -wq "^${KEY}" "${APP_PATH}/manifest"; then
  sed -i "s/^\(${KEY} *=\).*/\1 ${VAL}/" "${APP_PATH}/manifest"
else
  NUM=$(($(grep -o "^appname.*=" "${APP_PATH}/manifest" | wc -c) - 2))
  printf "%-*s= %s\n" "${NUM}" "${KEY}" "${VAL}" >>"${APP_PATH}/manifest"
fi

APPNAME=$(grep -w '^appname' "${APP_PATH}/manifest" | awk -F= '{print $2}' | xargs)
VERSION=$(grep -w '^version' "${APP_PATH}/manifest" | awk -F= '{print $2}' | xargs)
PLATFORM=$(grep -w '^platform' "${APP_PATH}/manifest" | awk -F= '{print $2}' | xargs)

tar -czf "$(dirname "${APP}")/${APPNAME}_${PLATFORM}_v${VERSION}.fpk" -C "${APP_PATH}" .

rm -rf "${APP_PATH}"
