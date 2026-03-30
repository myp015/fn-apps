#!/bin/bash

# ============================================================================
# File Name       : index.cgi
# Version         : 1.1.0
# Author          : FNOSP/xieguanru
# Collaborators   : FNOSP/MR_XIAOBO, RROrg/Ing
# Created         : 2025-11-18
# Last Modified   : 2026-03-29
# Description     : CGI script for serving static files and proxying API calls.
# License         : MIT
# ============================================================================

BASE_PATH="/var/apps/fn-scheduler/target/www"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQUEST_METHOD="$(printf '%s' "${REQUEST_METHOD:-GET}" | tr '[:lower:]' '[:upper:]')"

BODY_TMP=""
HDR_TMP=""
OUT_BODY=""

print_header() {
  printf '%s\r\n' "$1"
}

cleanup() {
  rm -f "$BODY_TMP" "$HDR_TMP" "$OUT_BODY"
}

trim_header_value() {
  local value="$1"
  value="${value#*:}"
  value="${value#"${value%%[![:space:]]*}"}"
  printf '%s' "${value%$'\r'}"
}

send_text_response() {
  local status="$1"
  local body="${2:-}"

  print_header "Status: $status"
  print_header "Content-Type: text/plain; charset=utf-8"
  print_header "Content-Length: ${#body}"
  print_header ""

  if [ "$REQUEST_METHOD" != "HEAD" ] && [ -n "$body" ]; then
    printf '%s' "$body"
  fi
  exit 0
}

send_empty_response() {
  print_header "Status: $1"
  print_header ""
  exit 0
}

create_temp_file() {
  mktemp 2>/dev/null || return 1
}

resolve_rel_path() {
  local uri_no_query rel

  uri_no_query="${REQUEST_URI%%\?*}"
  rel="/"
  case "$uri_no_query" in
    *index.cgi*)
      rel="${uri_no_query#*index.cgi}"
      ;;
  esac

  if [ -z "$rel" ] || [ "$rel" = "/" ]; then
    rel="/index.html"
  fi

  if [ "${rel#/}" = "$rel" ]; then
    rel="/$rel"
  fi

  case "$rel" in
    */)
      rel="${rel}index.html"
      ;;
  esac

  printf '%s' "$rel"
}

is_path_traversal() {
  local path="$1"
  local lower="${path,,}"

  case "$path" in
    ../* | */../* | */.. | ..)
      return 0
      ;;
  esac

  case "$lower" in
    *%2e%2e*)
      return 0
      ;;
  esac

  return 1
}

detect_mime() {
  local file_path="$1"
  local ext="${file_path##*.}"
  local ext_lc

  ext_lc="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
  case "$ext_lc" in
    html | htm) printf '%s' "text/html; charset=utf-8" ;;
    css) printf '%s' "text/css; charset=utf-8" ;;
    js) printf '%s' "application/javascript; charset=utf-8" ;;
    json) printf '%s' "application/json; charset=utf-8" ;;
    xml) printf '%s' "application/xml; charset=utf-8" ;;
    txt | log) printf '%s' "text/plain; charset=utf-8" ;;
    svg) printf '%s' "image/svg+xml" ;;
    jpg | jpeg) printf '%s' "image/jpeg" ;;
    png) printf '%s' "image/png" ;;
    gif) printf '%s' "image/gif" ;;
    webp) printf '%s' "image/webp" ;;
    ico) printf '%s' "image/x-icon" ;;
    *) printf '%s' "application/octet-stream" ;;
  esac
}

read_request_body() {
  if [ -z "${CONTENT_LENGTH:-}" ] || [ "$CONTENT_LENGTH" -le 0 ] 2>/dev/null; then
    return 0
  fi

  BODY_TMP="$(create_temp_file)" || send_text_response "500 Internal Server Error" "500 Internal Server Error: unable to create temp file"
  dd bs=1 count="$CONTENT_LENGTH" of="$BODY_TMP" 2>/dev/null || cat >"$BODY_TMP"
}

build_backend_url() {
  local rel_path="$1"
  local query_suffix=""

  if [ -n "${QUERY_STRING:-}" ]; then
    query_suffix="?${QUERY_STRING}"
  fi

  if [ -n "$BACKEND_UNIX_SOCKET" ] && [ -S "$BACKEND_UNIX_SOCKET" ]; then
    printf '%s' "http://localhost${rel_path}${query_suffix}"
  else
    printf '%s' "http://${BACKEND_HOST}:${BACKEND_PORT}${rel_path}${query_suffix}"
  fi
}

forward_request_headers() {
  local env_name header_name value

  for env_name in \
    CONTENT_TYPE \
    HTTP_AUTHORIZATION \
    REDIRECT_HTTP_AUTHORIZATION \
    HTTP_ACCEPT \
    HTTP_ACCEPT_LANGUAGE \
    HTTP_COOKIE \
    HTTP_USER_AGENT \
    HTTP_REFERER \
    HTTP_IF_NONE_MATCH \
    HTTP_IF_MODIFIED_SINCE
  do
    value="${!env_name}"
    [ -n "$value" ] || continue

    case "$env_name" in
      CONTENT_TYPE) header_name="Content-Type" ;;
      HTTP_AUTHORIZATION | REDIRECT_HTTP_AUTHORIZATION) header_name="Authorization" ;;
      HTTP_ACCEPT) header_name="Accept" ;;
      HTTP_ACCEPT_LANGUAGE) header_name="Accept-Language" ;;
      HTTP_COOKIE) header_name="Cookie" ;;
      HTTP_USER_AGENT) header_name="User-Agent" ;;
      HTTP_REFERER) header_name="Referer" ;;
      HTTP_IF_NONE_MATCH) header_name="If-None-Match" ;;
      HTTP_IF_MODIFIED_SINCE) header_name="If-Modified-Since" ;;
      *) continue ;;
    esac

    curl_args+=(-H "${header_name}: ${value}")
  done

  [ -n "${REMOTE_ADDR:-}" ] && curl_args+=(-H "X-Forwarded-For: ${REMOTE_ADDR}")
  [ -n "${REQUEST_SCHEME:-}" ] && curl_args+=(-H "X-Forwarded-Proto: ${REQUEST_SCHEME}")
}

parse_backend_response() {
  local line
  status_code="502"
  resp_ct="application/octet-stream"
  backend_headers=()

  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [ -n "$line" ] || continue

    case "$line" in
      HTTP/*)
        set -- $line
        status_code="${2:-502}"
        ;;
      [Cc]ontent-[Tt]ype:*)
        resp_ct="$(trim_header_value "$line")"
        ;;
      [Ss]et-[Cc]ookie:* | [Cc]ache-[Cc]ontrol:* | [Ee]xpires:* | [Aa]ccess-[Cc]ontrol-[Aa]llow-* | [Cc]ontent-[Dd]isposition:*)
        backend_headers+=("$line")
        ;;
    esac
  done <"$HDR_TMP"
}

proxy_api_request() {
  local backend_url curl_exit body_size
  BACKEND_UNIX_SOCKET="${BACKEND_UNIX_SOCKET:-${SCHEDULER_UNIX_SOCKET:-/usr/local/apps/@appdata/fn-scheduler/scheduler.sock}}"
  BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
  BACKEND_PORT="${BACKEND_PORT:-28256}"
  BACKEND_CONNECT_TIMEOUT="${BACKEND_CONNECT_TIMEOUT:-5}"
  BACKEND_MAX_TIME="${BACKEND_MAX_TIME:-30}"

  HDR_TMP="$(create_temp_file)" || send_text_response "500 Internal Server Error" "500 Internal Server Error: unable to create temp file"
  OUT_BODY="$(create_temp_file)" || send_text_response "500 Internal Server Error" "500 Internal Server Error: unable to create temp file"

  read_request_body

  curl_args=(
    -sS
    --http1.1
    --connect-timeout "$BACKEND_CONNECT_TIMEOUT"
    --max-time "$BACKEND_MAX_TIME"
    -D "$HDR_TMP"
    -o "$OUT_BODY"
    -X "$REQUEST_METHOD"
    -H "Connection: close"
  )
  forward_request_headers

  case "$REQUEST_METHOD" in
    POST | PUT | PATCH | DELETE)
      [ -n "$BODY_TMP" ] && curl_args+=(--data-binary "@$BODY_TMP")
      ;;
  esac

  backend_url="$(build_backend_url "$REL_PATH")"
  if [ -n "$BACKEND_UNIX_SOCKET" ] && [ -S "$BACKEND_UNIX_SOCKET" ]; then
    curl --unix-socket "$BACKEND_UNIX_SOCKET" "${curl_args[@]}" "$backend_url"
    curl_exit=$?
  else
    curl "${curl_args[@]}" "$backend_url"
    curl_exit=$?
  fi

  if [ "$curl_exit" -ne 0 ]; then
    if [ "$curl_exit" -eq 28 ]; then
      send_text_response "504 Gateway Timeout" "504 Gateway Timeout: Backend request timed out"
    fi
    send_text_response "502 Bad Gateway" "502 Bad Gateway: Backend unavailable"
  fi

  parse_backend_response

  print_header "Status: $status_code"
  print_header "Content-Type: $resp_ct"
  for header in "${backend_headers[@]}"; do
    print_header "$header"
  done

  if stat -c %s "$OUT_BODY" >/dev/null 2>&1; then
    body_size="$(stat -c %s "$OUT_BODY" 2>/dev/null || echo 0)"
    print_header "Content-Length: $body_size"
  fi
  print_header ""

  if [ "$REQUEST_METHOD" != "HEAD" ]; then
    cat "$OUT_BODY"
  fi
  exit 0
}

serve_static_file() {
  local target_file mime mtime size last_mod ims_epoch

  case "$REQUEST_METHOD" in
    GET | HEAD)
      ;;
    *)
      send_text_response "405 Method Not Allowed" "405 Method Not Allowed"
      ;;
  esac

  if is_path_traversal "${REL_PATH#/}"; then
    send_text_response "400 Bad Request" "Bad Request: Path traversal detected"
  fi

  target_file="${BASE_PATH}${REL_PATH}"
  if [ ! -f "$target_file" ]; then
    send_text_response "404 Not Found" "404 Not Found: ${REL_PATH}"
  fi

  mime="$(detect_mime "$target_file")"
  mtime=0
  size=0
  if stat -c %Y "$target_file" >/dev/null 2>&1; then
    mtime="$(stat -c %Y "$target_file" 2>/dev/null || echo 0)"
    size="$(stat -c %s "$target_file" 2>/dev/null || echo 0)"
  else
    size="$("$PYTHON_BIN" -c "import os,sys;print(os.path.getsize(sys.argv[1]))" "$target_file" 2>/dev/null || echo 0)"
    mtime="$("$PYTHON_BIN" -c "import os,sys;print(int(os.path.getmtime(sys.argv[1])))" "$target_file" 2>/dev/null || echo 0)"
  fi

  if [ -n "${HTTP_IF_MODIFIED_SINCE:-}" ]; then
    ims_epoch="$(date -d "$HTTP_IF_MODIFIED_SINCE" +%s 2>/dev/null || echo 0)"
    if [ "$mtime" -gt 0 ] && [ "$ims_epoch" -ge "$mtime" ]; then
      send_empty_response "304 Not Modified"
    fi
  fi

  last_mod="$(date -u -d "@$mtime" +"%a, %d %b %Y %H:%M:%S GMT" 2>/dev/null || date -u -r "$target_file" +"%a, %d %b %Y %H:%M:%S GMT" 2>/dev/null || echo "")"

  print_header "Content-Type: $mime"
  print_header "Content-Length: $size"
  [ -n "$last_mod" ] && print_header "Last-Modified: $last_mod"
  print_header ""

  if [ "$REQUEST_METHOD" != "HEAD" ]; then
    cat "$target_file"
  fi
}

trap cleanup EXIT

REL_PATH="$(resolve_rel_path)"

case "$REL_PATH" in
  /api | /api/*)
    proxy_api_request
    ;;
  *)
    serve_static_file
    ;;
esac
