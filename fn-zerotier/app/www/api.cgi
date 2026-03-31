#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qs

SERVICE_NAME = "zerotier-one"


def respond(obj):
    print("Content-Type: application/json")
    print()
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


def command_exists(name):
    return shutil.which(name) is not None


def get_query_param(name):
    values = parse_qs(os.environ.get("QUERY_STRING", ""), keep_blank_values=True).get(
        name
    )
    return values[0] if values else None


def read_post_json():
    if os.environ.get("REQUEST_METHOD", "").upper() != "POST":
        return {}
    try:
        length = int(os.environ.get("CONTENT_LENGTH", "0") or 0)
    except ValueError:
        length = 0
    body = sys.stdin.read(length) if length else ""
    if not body:
        return {}
    try:
        return json.loads(body)
    except Exception:
        return {}


def run_cmd(cmd, timeout=20):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode
    except Exception as exc:
        return False, "", str(exc), -1


def systemctl_available():
    return command_exists("systemctl")


def service_active():
    if not systemctl_available():
        return False
    return (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", SERVICE_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def service_enabled():
    if not systemctl_available():
        return False
    return (
        subprocess.run(
            ["systemctl", "is-enabled", "--quiet", SERVICE_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def zerotier_json(*args):
    if not command_exists("zerotier-cli"):
        return None, "zerotier-cli not found"
    ok, out, err, _ = run_cmd(["zerotier-cli", "-j", *args])
    if not ok:
        return None, (err or out or "command failed").strip()
    try:
        return json.loads(out or "null"), None
    except Exception as exc:
        return None, f"invalid json: {exc}"


def collect_status():
    installed = command_exists("zerotier-cli")
    info, info_error = (None, None)
    networks, networks_error = ([], None)
    peers, peers_error = ([], None)

    if installed:
        info, info_error = zerotier_json("info")
        networks, networks_error = zerotier_json("listnetworks")
        peers, peers_error = zerotier_json("listpeers")
        if not isinstance(networks, list):
            networks = []
        if not isinstance(peers, list):
            peers = []

    online_peers = sum(
        1
        for peer in peers
        if isinstance(peer.get("latency"), int) and peer.get("latency", -1) >= 0
    )

    return {
        "ok": True,
        "service": {
            "name": SERVICE_NAME,
            "installed": installed,
            "active": service_active(),
            "enabled": service_enabled(),
        },
        "info": info or {},
        "networks": networks,
        "peers": peers[:12],
        "peerSummary": {
            "total": len(peers),
            "online": online_peers,
        },
        "errors": {
            "info": info_error,
            "networks": networks_error,
            "peers": peers_error,
        },
    }


def validate_network_id(network_id):
    value = (network_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16}", value):
        respond({"ok": False, "error": "network id 必须是 16 位十六进制字符串"})
    return value


def ensure_service_active():
    if not systemctl_available():
        respond({"ok": False, "error": "systemctl 不可用"})
    if not service_active():
        respond({"ok": False, "error": "ZeroTier 服务未启动，请先在应用中启动服务"})


def handle_join(body):
    network_id = validate_network_id(body.get("network"))
    ensure_service_active()
    ok, out, err, _ = run_cmd(["zerotier-cli", "join", network_id])
    if not ok:
        respond({"ok": False, "error": (err or out or "join failed").strip()})
    payload = collect_status()
    payload["message"] = f"已发起加入网络 {network_id}"
    respond(payload)


def handle_leave(body):
    network_id = validate_network_id(body.get("network"))
    ok, out, err, _ = run_cmd(["zerotier-cli", "leave", network_id])
    if not ok:
        respond({"ok": False, "error": (err or out or "leave failed").strip()})
    payload = collect_status()
    payload["message"] = f"已离开网络 {network_id}"
    respond(payload)


def handle_network_set(body):
    network_id = validate_network_id(body.get("network"))
    settings = body.get("settings") or {}
    if not isinstance(settings, dict) or not settings:
        respond({"ok": False, "error": "没有需要更新的网络设置"})

    args = ["zerotier-cli", "set", network_id]
    for key in ("allowManaged", "allowGlobal", "allowDefault", "allowDNS"):
        if key in settings:
            args.append(f"{key}={'1' if bool(settings[key]) else '0'}")

    if len(args) <= 3:
        respond({"ok": False, "error": "没有有效的网络设置"})

    ok, out, err, _ = run_cmd(args)
    if not ok:
        respond({"ok": False, "error": (err or out or "更新网络设置失败").strip()})
    payload = collect_status()
    payload["message"] = f"网络 {network_id} 设置已更新"
    respond(payload)


def main():
    action = (get_query_param("action") or "status").strip().lower()
    if action == "status":
        respond(collect_status())

    body = read_post_json()
    if action == "join":
        handle_join(body)
    if action == "leave":
        handle_leave(body)
    if action == "network_set":
        handle_network_set(body)

    respond({"ok": False, "error": "unknown action"})


if __name__ == "__main__":
    main()
