#!/bin/bash

BASE_PATH="/var/apps/fn-vgmng/target/www"
URI_NO_QUERY="${REQUEST_URI%%\?*}"
REL_PATH="/"

case "${URI_NO_QUERY}" in
  *index.cgi*)
    REL_PATH="${URI_NO_QUERY#*index.cgi}"
    ;;
esac

if [ -z "${REL_PATH}" ] || [ "${REL_PATH}" = "/" ]; then
  REL_PATH="/index.html"
fi

TARGET_FILE="${BASE_PATH}${REL_PATH}"

if echo "${TARGET_FILE}" | grep -q '\.\.'; then
  echo "Status: 400 Bad Request"
  echo "Content-Type: text/plain; charset=utf-8"
  echo ""
  echo "Bad Request: Path traversal detected"
  exit 0
fi

if [ ! -f "${TARGET_FILE}" ]; then
  echo "Status: 404 Not Found"
  echo "Content-Type: text/plain; charset=utf-8"
  echo ""
  echo "404 Not Found: ${REL_PATH}"
  exit 0
fi

ext="${TARGET_FILE##*.}"
ext_lc="$(printf '%s' "${ext}" | tr '[:upper:]' '[:lower:]')"

case "${ext_lc}" in
  html | htm)
    mime="text/html; charset=utf-8"
    ;;
  css)
    mime="text/css; charset=utf-8"
    ;;
  js)
    mime="application/javascript; charset=utf-8"
    ;;
  png)
    mime="image/png"
    ;;
  jpg | jpeg)
    mime="image/jpeg"
    ;;
  svg)
    mime="image/svg+xml"
    ;;
  json)
    mime="application/json; charset=utf-8"
    ;;
  *)
    mime="application/octet-stream"
    ;;
esac

size=$(stat -c %s "${TARGET_FILE}" 2>/dev/null || echo 0)
printf 'Content-Type: %s\r\n' "${mime}"
printf 'Content-Length: %s\r\n' "${size}"
printf '\r\n'

if [ "${REQUEST_METHOD:-GET}" = "HEAD" ]; then
  exit 0
fi

cat "${TARGET_FILE}"
