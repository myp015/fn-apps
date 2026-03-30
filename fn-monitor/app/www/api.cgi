#!/usr/bin/env python3
import glob
import json
import os
import re
import subprocess
import sys
from urllib.parse import parse_qs

LOGIND_PATH = "/etc/systemd/logind.conf"
BASE_DIR = os.path.dirname(__file__)
DRM_CLASS_PATH = "/sys/class/drm"
FB_CLASS_GLOB = "/sys/class/graphics/fb*/blank"


def respond(obj, status=200):
    print("Content-Type: application/json")
    print()
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.exit(0)


def read_logind():
    try:
        with open(LOGIND_PATH, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception as e:
        respond({"ok": False, "error": "read failed: " + str(e)})
    # parse simple key=value pairs (no full ini parsing)
    parsed = {}
    section = None
    for raw in txt.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and "]" in line:
            section = line[1 : line.index("]")].strip()
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            parsed[k.strip()] = v.strip()
    respond({"ok": True, "content": txt, "parsed": parsed})


def write_logind(body):
    # body may contain 'content' (full file) or 'changes' dict
    content = body.get("content")
    if content is None:
        # apply changes to file textually
        try:
            with open(LOGIND_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            respond({"ok": False, "error": "read before write failed: " + str(e)})
        changes = body.get("changes", {})
        out_lines = []
        section = None
        applied = set()
        login_out_index = None
        for ln in lines:
            s = ln.strip()
            if s.startswith("[") and "]" in s:
                section = s[1 : s.index("]")].strip()
                out_lines.append(ln)
                if section == "Login" and login_out_index is None:
                    # record insertion point (after the [Login] header)
                    login_out_index = len(out_lines)
                continue
            if "=" in ln and section == "Login":
                k = ln.split("=", 1)[0].strip()
                if k in changes:
                    # empty string or null means remove the key to restore system default
                    val = changes[k]
                    if val is None or (isinstance(val, str) and val == ""):
                        # skip this line (remove the setting)
                        applied.add(k)
                    else:
                        out_lines.append(f"{k}={changes[k]}\n")
                        applied.add(k)
                else:
                    out_lines.append(ln)
            else:
                out_lines.append(ln)
        # append missing keys under [Login]
        # Only add missing keys that have a non-empty value (empty means remove/default)
        missing = [
            k
            for k in changes.keys()
            if k not in applied
            and (
                changes[k] is not None
                and not (isinstance(changes[k], str) and changes[k] == "")
            )
        ]
        if missing:
            if login_out_index is not None:
                # insert missing keys right after the existing [Login] header
                insert_lines = [f"{k}={changes[k]}\n" for k in missing]
                out_lines[login_out_index:login_out_index] = insert_lines
            else:
                out_lines.append("\n[Login]\n")
                for k in missing:
                    out_lines.append(f"{k}={changes[k]}\n")
        content = "".join(out_lines)
    try:
        with open(LOGIND_PATH, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        respond({"ok": False, "error": "write failed: " + str(e)})
    if body.get("apply"):
        try:
            p = subprocess.run(
                ["systemctl", "restart", "systemd-logind"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if p.returncode != 0:
                respond(
                    {
                        "ok": False,
                        "error": "restart failed",
                        "stdout": p.stdout,
                        "stderr": p.stderr,
                    }
                )
            else:
                respond(
                    {"ok": True, "message": "written and restarted", "stdout": p.stdout}
                )
        except Exception as e:
            respond({"ok": False, "error": "restart exception: " + str(e)})
    respond({"ok": True, "message": "written"})


def get_query_param(name):
    values = parse_qs(
        os.environ.get("QUERY_STRING", ""),
        keep_blank_values=True,
    ).get(name)
    if not values:
        return None
    return values[0]


def read_post_json():
    try:
        if os.environ.get("REQUEST_METHOD", "").upper() != "POST":
            return {}
        cl = int(os.environ.get("CONTENT_LENGTH", "0") or 0)
        body = sys.stdin.read(cl) if cl else ""
        if not body:
            return {}
        return json.loads(body)
    except Exception:
        return {}


def run_cmd(cmd, timeout=8):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, "", str(e)


def list_framebuffer_blank_paths(writable_only=False):
    paths = []
    for path in sorted(glob.glob(FB_CLASS_GLOB)):
        if not os.path.exists(path):
            continue
        if writable_only and not os.access(path, os.W_OK):
            continue
        paths.append(path)
    return paths


def read_framebuffer_blank_state():
    values = []
    for path in list_framebuffer_blank_paths(writable_only=False):
        raw = read_text_file(path)
        if raw is None:
            continue
        raw = raw.strip()
        if not raw:
            continue
        try:
            values.append(int(raw))
        except ValueError:
            continue
    if not values:
        return None
    return max(values)


def set_framebuffer_blank(turn_on):
    blank_paths = list_framebuffer_blank_paths(writable_only=True)
    if not blank_paths:
        return False, "fb_blank", "no writable framebuffer blank control"

    target_value = "0" if turn_on else "4"
    errors = []
    written = []
    for path in blank_paths:
        ok, msg = write_text_file(path, target_value)
        if ok:
            written.append(path)
        else:
            errors.append(f"{path}: {msg}")

    if not written:
        return False, "fb_blank", "; ".join(errors) or "framebuffer blank failed"

    detail = f"wrote {target_value} to " + ", ".join(written)
    if errors:
        detail += " (partial errors: " + "; ".join(errors) + ")"
    return True, "fb_blank", detail


def list_drm_connectors():
    connectors = []
    for path in sorted(glob.glob(os.path.join(DRM_CLASS_PATH, "card*-*"))):
        base = os.path.basename(path)
        if "-renderD" in base or "-controlD" in base:
            continue
        status_path = os.path.join(path, "status")
        if not os.path.exists(status_path):
            continue
        # e.g. card0-HDMI-A-1 => card=card0, name=HDMI-A-1
        parts = base.split("-", 1)
        if len(parts) != 2:
            continue
        connectors.append(
            {
                "sys_name": base,
                "card": parts[0],
                "name": parts[1],
                "path": path,
            }
        )
    return connectors


def detect_card_module(card_name):
    mod_link = os.path.join(DRM_CLASS_PATH, card_name, "device", "driver", "module")
    try:
        if os.path.islink(mod_link):
            target = os.readlink(mod_link)
            return os.path.basename(target)
    except Exception:
        pass
    return None


def detect_card_modules(connectors):
    modules = {}
    for item in connectors:
        card_name = item.get("card")
        if not card_name:
            continue
        if card_name not in modules:
            modules[card_name] = detect_card_module(card_name)
    return modules


def parse_modetest_connectors(module_name=None):
    # Return mapping: connector_id(int) -> {id, name, dpms_value}
    # Use auto-scan mode by default (without -M) to avoid driver/module mapping issues.
    cmd = (
        ["modetest", "-c"] if not module_name else ["modetest", "-M", module_name, "-c"]
    )
    rc, out, err = run_cmd(cmd, timeout=12)
    if rc != 0:
        return {"_error": f"modetest failed: {err.strip() or out.strip()}"}

    by_id = {}
    lines = out.splitlines()
    cur_id = None
    in_props = False
    seen_dpms = False
    cur_item = None

    # connector row examples may vary; parse by token positions conservatively
    row_re = re.compile(r"^\s*(\d+)\s+\d+\s+\w+\s+([A-Za-z0-9._:-]+)\b")
    for line in lines:
        m = row_re.match(line)
        if m:
            cur_id = int(m.group(1))
            cur_item = {"id": cur_id, "name": m.group(2), "dpms_value": None}
            by_id[cur_id] = cur_item
            in_props = False
            seen_dpms = False
            continue

        s = line.strip()
        if not cur_item:
            continue
        if s == "props:" or s.startswith("props:"):
            in_props = True
            continue
        if not in_props:
            continue

        if re.match(r"^\d+\s+DPMS:\s*$", s):
            seen_dpms = True
            continue
        if seen_dpms:
            vm = re.match(r"^value:\s*(\d+)\s*$", s)
            if vm:
                cur_item["dpms_value"] = int(vm.group(1))
                seen_dpms = False

    return by_id


def find_modetest_connector(parsed, connector_id=None, connector_name=None):
    if not isinstance(parsed, dict) or "_error" in parsed:
        return None
    if connector_id is not None:
        match = parsed.get(connector_id)
        if match:
            return match
    if connector_name:
        for value in parsed.values():
            if isinstance(value, dict) and value.get("name") == connector_name:
                return value
    return None


def read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def write_text_file(path, value):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(value)
        return True, "ok"
    except Exception as e:
        return False, str(e)


def build_drm_status():
    connectors = list_drm_connectors()
    auto_parsed = parse_modetest_connectors(None)
    card_modules = detect_card_modules(connectors)
    parsed_by_module = {}
    for module_name in sorted(
        {m for m in card_modules.values() if isinstance(m, str) and m}
    ):
        parsed_by_module[module_name] = parse_modetest_connectors(module_name)

    fb_blank_paths = list_framebuffer_blank_paths(writable_only=True)
    fb_blank_state = read_framebuffer_blank_state()

    result = []
    for c in connectors:
        status = read_text_file(os.path.join(c["path"], "status")) or "unknown"
        enabled_path = os.path.join(c["path"], "enabled")
        enabled = read_text_file(enabled_path)
        connector_id_txt = read_text_file(os.path.join(c["path"], "connector_id"))
        if enabled is None:
            enabled = "unknown"
        connector_id = None
        if connector_id_txt and connector_id_txt.isdigit():
            connector_id = int(connector_id_txt)

        item = {
            "sys_name": c["sys_name"],
            "card": c["card"],
            "name": c["name"],
            "path": c["path"],
            "status": status,
            "enabled": enabled,
            "enabled_writable": os.path.exists(enabled_path)
            and os.access(enabled_path, os.W_OK),
            "module": card_modules.get(c["card"]),
            "connector_id": connector_id,
            "dpms": None,
            "fb_blank": fb_blank_state,
        }

        modetest_errors = []
        parsed_candidates = []
        if item["module"] and item["module"] in parsed_by_module:
            parsed_candidates.append(parsed_by_module[item["module"]])
        parsed_candidates.append(auto_parsed)

        for parsed in parsed_candidates:
            if not isinstance(parsed, dict):
                continue
            if "_error" in parsed:
                modetest_errors.append(parsed["_error"])
                continue
            mt = find_modetest_connector(parsed, connector_id, c["name"])
            if mt:
                dpms_v = mt.get("dpms_value")
                if dpms_v is not None:
                    item["dpms"] = dpms_v
                    break

        if modetest_errors:
            item["modetest_error"] = " ; ".join(dict.fromkeys(modetest_errors))

        item["dpms_supported"] = item["dpms"] is not None

        # derive unified power state from hardware
        # preference: DPMS if available; fallback to enabled/status
        if item["dpms"] is not None:
            item["state"] = "on" if item["dpms"] == 0 else "off"
        elif enabled in ("enabled", "disabled"):
            item["state"] = "on" if enabled == "enabled" else "off"
        elif status == "connected":
            item["state"] = "unknown"
        else:
            item["state"] = "disconnected"

        result.append(item)

    connected_count = sum(1 for item in result if item.get("status") == "connected")
    fb_blank_supported = bool(fb_blank_paths) and connected_count <= 1

    for item in result:
        control_methods = []
        if item["dpms_supported"]:
            control_methods.append("dpms")
        if item["enabled_writable"]:
            control_methods.append("sysfs_enabled")
        if item.get("status") == "connected" and fb_blank_supported:
            control_methods.append("fb_blank")
        item["control_methods"] = control_methods
        item["fb_blank_supported"] = "fb_blank" in control_methods

        if item["dpms"] is None and item["fb_blank_supported"] and fb_blank_state is not None:
            item["state"] = "off" if fb_blank_state > 0 else "on"

        if item["status"] != "connected":
            item["controllable"] = False
            item["control_reason"] = "显示器未连接"
        elif control_methods:
            item["controllable"] = True
            item["control_reason"] = ""
        else:
            item["controllable"] = False
            if "modetest_error" in item:
                item["control_reason"] = (
                    "未发现可用控制属性（DPMS不可用、sysfs不可写、fb_blank不可用）; "
                    + item["modetest_error"]
                )
            else:
                item["control_reason"] = "未发现可用控制属性（DPMS不可用、sysfs不可写、fb_blank不可用）"

    return result


def resolve_target_connector(name_or_sys):
    all_items = build_drm_status()
    if not all_items:
        return None, all_items

    if name_or_sys:
        for it in all_items:
            if it["sys_name"] == name_or_sys or it["name"] == name_or_sys:
                return it, all_items
        return None, all_items

    # default: first connected connector, else first one
    for it in all_items:
        if it.get("status") == "connected":
            return it, all_items
    return all_items[0], all_items


def set_connector_dpms(target, turn_on):
    if not target:
        return False, "dpms", "no target connector"
    connector_id = target.get("connector_id")
    if connector_id is None:
        return False, "dpms", "connector has no DPMS property"

    attempts = []
    value = 0 if turn_on else 3
    module_name = (target.get("module") or "").strip()
    commands = []
    if module_name:
        commands.append(
            (
                f"dpms_modetest_{module_name}",
                ["modetest", "-M", module_name, "-w", f"{connector_id}:DPMS:{value}"],
            )
        )
    commands.append(("dpms_modetest_auto", ["modetest", "-w", f"{connector_id}:DPMS:{value}"]))

    seen = set()
    for method, cmd in commands:
        cmd_key = tuple(cmd)
        if cmd_key in seen:
            continue
        seen.add(cmd_key)
        rc, out, err = run_cmd(cmd, timeout=10)
        if rc == 0:
            return True, method, "ok"
        attempts.append(f"{method}: {err.strip() or out.strip() or 'modetest set DPMS failed'}")

    return False, "dpms", "; ".join(attempts) or "modetest set DPMS failed"


def set_connector_sysfs_enabled(target, turn_on):
    if not target:
        return False, "sysfs_enabled", "no target connector"
    connector_path = target.get("path")
    if not connector_path:
        connector_path = os.path.join(DRM_CLASS_PATH, target.get("sys_name", ""))
    enabled_path = os.path.join(connector_path, "enabled")
    if not os.path.exists(enabled_path):
        return False, "sysfs_enabled", "connector has no sysfs enabled control"

    # kernel expects string values in this file
    target_value = "enabled" if turn_on else "disabled"
    ok, msg = write_text_file(enabled_path, target_value)
    if not ok:
        return False, "sysfs_enabled", f"write {enabled_path} failed: {msg}"

    now = read_text_file(enabled_path)
    if now not in ("enabled", "disabled"):
        return False, "sysfs_enabled", f"unexpected sysfs enabled value: {now}"
    if (turn_on and now != "enabled") or ((not turn_on) and now != "disabled"):
        return False, "sysfs_enabled", f"sysfs enabled verify failed: {now}"
    return True, "sysfs_enabled", "ok"


def set_connector_power(target, turn_on, all_items=None):
    errors = []

    # 1) try DPMS via modetest (preferred)
    ok, method, message = set_connector_dpms(target, turn_on)
    if ok:
        return True, method, "ok"
    errors.append(f"dpms failed: {message}")

    # 2) fallback to sysfs enabled toggle
    ok2, method2, message2 = set_connector_sysfs_enabled(target, turn_on)
    if ok2:
        return True, method2, "ok"
    errors.append(f"sysfs fallback failed: {message2}")

    # 3) final fallback: framebuffer blank for single-display environments
    connected_count = 0
    if isinstance(all_items, list):
        connected_count = sum(
            1 for item in all_items if item.get("status") == "connected"
        )
    if connected_count <= 1:
        ok3, method3, message3 = set_framebuffer_blank(turn_on)
        if ok3:
            return True, method3, message3
        errors.append(f"fb blank fallback failed: {message3}")
    else:
        errors.append("fb blank fallback skipped: multiple connected displays")

    return False, "none", "; ".join(errors)


def main():
    action = get_query_param("action") or "read"
    if action == "read":
        return read_logind()
    if action == "write":
        body = read_post_json()
        return write_logind(body)
    if action == "screen":
        # DRM-only connector power control (no X environment)
        state = (get_query_param("state") or "").lower().strip()
        target_name = (get_query_param("connector") or "").strip()
        if state not in ("on", "off"):
            respond({"ok": False, "error": "state must be 'on' or 'off'"})

        target, all_items = resolve_target_connector(target_name)
        if not target:
            respond(
                {
                    "ok": False,
                    "error": "connector not found",
                    "connector": target_name,
                    "connectors": all_items,
                }
            )

        if not target.get("controllable"):
            respond(
                {
                    "ok": False,
                    "error": target.get("control_reason")
                    or "target connector is not controllable",
                    "target": target,
                }
            )

        ok, method, message = set_connector_power(
            target,
            turn_on=(state == "on"),
            all_items=all_items,
        )
        if not ok:
            respond(
                {
                    "ok": False,
                    "error": message,
                    "target": target,
                }
            )

        refreshed = build_drm_status()
        respond(
            {
                "ok": True,
                "message": f"screen {state}",
                "detail": message,
                "method": method,
                "target": target.get("sys_name"),
                "connectors": refreshed,
            }
        )

    if action == "screen_status":
        # real hardware status via DRM/sysfs
        try:
            connectors = build_drm_status()
            target_name = (get_query_param("connector") or "").strip()
            selected = None
            if target_name:
                for it in connectors:
                    if (
                        it.get("sys_name") == target_name
                        or it.get("name") == target_name
                    ):
                        selected = it
                        break
            if selected is None and connectors:
                selected = next(
                    (it for it in connectors if it.get("status") == "connected"),
                    connectors[0],
                )
            respond(
                {
                    "ok": True,
                    "connector": selected.get("sys_name") if selected else None,
                    "state": selected.get("state") if selected else None,
                    "connectors": connectors,
                }
            )
        except Exception as e:
            respond({"ok": False, "error": "read DRM status failed: " + str(e)})
    respond({"ok": False, "error": "unknown action"})


if __name__ == "__main__":
    main()
