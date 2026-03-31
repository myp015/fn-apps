#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Lightweight scheduler backend with REST API and static file hosting."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import signal
import socket
import sqlite3
import threading
import tempfile
import time
from datetime import datetime, timedelta, timezone

from typing import Any, Callable, Dict, List, Optional, Set
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from subprocess import PIPE, Popen, TimeoutExpired, run

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - non-POSIX systems
    grp = None  # type: ignore
    pwd = None  # type: ignore
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit, unquote

###############################################################################
# Helpers and configuration
###############################################################################

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 28256
DEFAULT_SOCKET_PATH = os.path.join(ROOT_DIR, "fn-scheduler.sock")
DEFAULT_DB_PATH = os.path.join(ROOT_DIR, "scheduler.db")
DEFAULT_SETTINGS_PATH = os.path.join(ROOT_DIR, "scheduler.settings.json")
DB_LATEST_VERSION = 2

TASK_TIMEOUT = int(os.environ.get("SCHEDULER_TASK_TIMEOUT", "900"))
CONDITION_TIMEOUT = int(os.environ.get("SCHEDULER_CONDITION_TIMEOUT", "60"))
MAX_LOOKAHEAD_MINUTES = 60 * 24 * 366  # one leap year
RESULT_LOG_PREVIEW_LIMIT = int(os.environ.get("SCHEDULER_RESULT_LOG_PREVIEW_LIMIT", "4000"))
RESULT_RETENTION_PER_TASK = int(
    os.environ.get("SCHEDULER_RESULT_RETENTION_PER_TASK", "200")
)
EVENT_TYPE_SCRIPT = "script"
EVENT_TYPE_BOOT = "system_boot"
EVENT_TYPE_SHUTDOWN = "system_shutdown"
EVENT_TYPES = {EVENT_TYPE_SCRIPT, EVENT_TYPE_BOOT, EVENT_TYPE_SHUTDOWN}
QUIET_ACCESS_LOG_PATHS = {"/api/tasks", "/api/health", "/api/tasks/version"}
LOG_POLLING_REQUESTS = os.environ.get("SCHEDULER_LOG_POLLING_REQUESTS", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _detect_default_account() -> str:
    for env_key in ("SCHEDULER_DEFAULT_ACCOUNT", "USERNAME", "USER"):
        value = os.environ.get(env_key)
        if value:
            return value
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - fallback only
        return "current_user"


DEFAULT_ACCOUNT_NAME = _detect_default_account()
ALLOWED_ACCOUNT_GIDS = (0, 1000, 1001)
POSIX_ACCOUNT_SUPPORT = os.name == "posix" and pwd is not None and grp is not None


def normalize_base_path(raw: Optional[str]) -> str:
    base = (raw or "/").strip()
    if not base:
        base = "/"
    if not base.startswith("/"):
        base = f"/{base}"
    if len(base) > 1 and base.endswith("/"):
        base = base.rstrip("/")
    return base or "/"


def strip_wrapping_quotes(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {'"', "'"}:
        return trimmed[1:-1]
    return trimmed


def serialize_result_row(
    row: Dict[str, Any], include_log: bool = True, log_limit: Optional[int] = None
) -> Dict[str, Any]:
    payload = dict(row)
    log_text = payload.get("log") or ""
    if not isinstance(log_text, str):
        log_text = str(log_text)

    log_size = len(log_text)
    if log_limit is not None and log_limit >= 0:
        log_preview = log_text[:log_limit]
        log_truncated = log_size > log_limit
    else:
        log_preview = log_text
        log_truncated = False

    payload["log_size"] = log_size
    payload["log_preview"] = log_preview
    payload["log_truncated"] = log_truncated

    if include_log:
        payload["log"] = log_text
    else:
        payload.pop("log", None)

    return payload


class SchedulerSettings:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data = {
            "task_timeout": TASK_TIMEOUT,
            "condition_timeout": CONDITION_TIMEOUT,
            "result_log_preview_limit": RESULT_LOG_PREVIEW_LIMIT,
            "result_retention_per_task": RESULT_RETENTION_PER_TASK,
        }
        self._load()

    def _sanitize(self, raw: Dict[str, Any]) -> Dict[str, int]:
        data = dict(self._data)

        def _read_int(key: str, minimum: int) -> None:
            if key not in raw:
                return
            value = int(raw[key])
            if value < minimum:
                raise ValueError(f"{key} must be >= {minimum}")
            data[key] = value

        _read_int("task_timeout", 0)
        _read_int("condition_timeout", 1)
        _read_int("result_log_preview_limit", 256)
        _read_int("result_retention_per_task", 0)
        return data

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, dict):
                with self._lock:
                    self._data = self._sanitize(loaded)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to load scheduler settings from %s: %s", self.path, exc)

    def _save(self) -> None:
        settings_dir = os.path.dirname(self.path)
        if settings_dir:
            os.makedirs(settings_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="scheduler-settings-", suffix=".json", dir=settings_dir or None)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def to_dict(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._data)

    def update(self, raw: Dict[str, Any]) -> Dict[str, int]:
        with self._lock:
            self._data = self._sanitize(raw)
            self._save()
            return dict(self._data)

    @property
    def task_timeout(self) -> int:
        with self._lock:
            return int(self._data["task_timeout"])

    @property
    def condition_timeout(self) -> int:
        with self._lock:
            return int(self._data["condition_timeout"])

    @property
    def result_log_preview_limit(self) -> int:
        with self._lock:
            return int(self._data["result_log_preview_limit"])

    @property
    def result_retention_per_task(self) -> int:
        with self._lock:
            return int(self._data["result_retention_per_task"])


logger = logging.getLogger("fn_scheduler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def time_now() -> datetime:
    IS_LOCAL_TIME = True
    if IS_LOCAL_TIME:
        # 返回本地时间（无时区信息，和服务器系统时间一致）
        return datetime.now()
    else:
        # 带时区信息的 UTC 时间
        return datetime.now(timezone.utc)


def isoformat(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    # 直接用本地时间的 ISO 格式
    return dt.replace(microsecond=0).isoformat(sep=" ")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # 兼容带空格的本地时间字符串
        dt = datetime.fromisoformat(value.replace("T", " "))
        # 如果是带时区的，转为本地无时区
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def list_allowed_accounts() -> List[str]:
    """Return distinct account names whose primary or supplemental group is allowed."""

    if not POSIX_ACCOUNT_SUPPORT:
        return [DEFAULT_ACCOUNT_NAME] if DEFAULT_ACCOUNT_NAME else []

    accounts: Set[str] = set()
    try:
        for entry in pwd.getpwall():  # type: ignore[attr-defined]
            if entry.pw_gid in ALLOWED_ACCOUNT_GIDS:
                accounts.add(entry.pw_name)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to enumerate passwd entries: %s", exc)

    for gid in ALLOWED_ACCOUNT_GIDS:
        try:
            group = grp.getgrgid(gid)  # type: ignore[attr-defined]
        except KeyError:
            continue
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to read group %s: %s", gid, exc)
            continue
        for member in group.gr_mem:
            if member:
                accounts.add(member)

    return sorted(accounts)


def ensure_account_allowed(account: str) -> str:
    allowed = list_allowed_accounts()
    if not allowed:
        if POSIX_ACCOUNT_SUPPORT:
            raise ValueError("no allowed accounts found in system groups 0/1000/1001")
        raise ValueError("current system cannot determine default account")
    if not POSIX_ACCOUNT_SUPPORT:
        default_account = allowed[0]
        if account and account != default_account:
            raise ValueError(
                f"Windows environment only supports using account {default_account}"
            )
        return default_account
    if account not in allowed:
        raise ValueError("account must belong to system groups 0/1000/1001")
    return account


###############################################################################
# Cron expression parsing
###############################################################################


class CronExpression:
    """Minimal 5-field cron parser supporting ranges, lists, and steps."""

    FIELD_SPECS = (
        ("minute", 0, 59, 60),
        ("hour", 0, 23, 24),
        ("day", 1, 31, 31),
        ("month", 1, 12, 12),
        ("weekday", 0, 6, 7),
    )

    def __init__(self, expression: str):
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError("Cron expression must contain 5 fields")
        self.fields: List[List[int]] = []
        self._wildcards: List[bool] = []
        for part, spec in zip(parts, self.FIELD_SPECS):
            expanded, wildcard = self._expand_field(part, spec)
            self.fields.append(expanded)
            self._wildcards.append(wildcard)

    def _expand_field(self, token: str, spec: tuple) -> tuple[List[int], bool]:
        name, min_value, max_value, span = spec
        values: set[int] = set()
        wildcard = False
        items = token.split(",")
        for raw_item in items:
            original_item = raw_item.strip() or "*"
            item = original_item
            step = 1
            if "/" in original_item:
                base, step_str = original_item.split("/", 1)
                item = base or "*"
                step = int(step_str)
                if step <= 0:
                    raise ValueError(f"Invalid step for {name}")
            expanded = self._expand_range(item, min_value, max_value)
            if not expanded:
                raise ValueError(f"Invalid {name} segment: {item}")
            start_val = expanded[0]
            for value in expanded:
                if (value - start_val) % step == 0:
                    values.add(value)
            wildcard = wildcard or (original_item == "*")
        if not values:
            raise ValueError(f"No values computed for {name}")
        if name == "weekday":
            normalized = set()
            for val in values:
                normalized.add(0 if val == 7 else val)
            values = normalized
        if not all(min_value <= v <= max_value for v in values):
            raise ValueError(f"{name} values out of range")
        full_span = len(values) == span
        return sorted(values), (wildcard or full_span)

    def _expand_range(self, item: str, min_value: int, max_value: int) -> List[int]:
        if item == "*":
            return list(range(min_value, max_value + 1))
        if item.isdigit():
            return [int(item)]
        if "-" in item:
            start_str, end_str = item.split("-", 1)
            start = int(start_str)
            end = int(end_str)
            if start > end:
                raise ValueError("Cron range start greater than end")
            return list(range(start, end + 1))
        raise ValueError("Unsupported cron token")

    def next_after(self, moment: datetime) -> datetime:
        base = moment.replace(second=0, microsecond=0)
        candidate = base
        for _ in range(MAX_LOOKAHEAD_MINUTES):
            candidate += timedelta(minutes=1)
            if self._matches(candidate):
                return candidate
        raise ValueError("Unable to compute next run within lookahead window")

    def _matches(self, candidate: datetime) -> bool:
        minute, hour = candidate.minute, candidate.hour
        day, month = candidate.day, candidate.month
        weekday = candidate.weekday()
        dom_match = day in self.fields[2]
        dow_match = weekday in self.fields[4]
        dom_wildcard = self._wildcards[2]
        dow_wildcard = self._wildcards[4]

        if dom_wildcard and dow_wildcard:
            calendar_ok = True
        elif dom_wildcard:
            calendar_ok = dow_match
        elif dow_wildcard:
            calendar_ok = dom_match
        else:
            calendar_ok = dom_match or dow_match

        return (
            minute in self.fields[0]
            and hour in self.fields[1]
            and month in self.fields[3]
            and calendar_ok
        )


###############################################################################
# Database layer
###############################################################################


class Database:
    def __init__(self, path: str, result_retention_per_task: int = RESULT_RETENTION_PER_TASK):
        self.path = path
        self.result_retention_per_task = result_retention_per_task
        db_dir = os.path.dirname(path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA user_version;")
            (version,) = cur.fetchone()
            if version < 1:
                self._create_schema(cur)
                version = DB_LATEST_VERSION
                cur.execute(f"PRAGMA user_version={DB_LATEST_VERSION};")
            if version < 2:
                try:
                    cur.execute(
                        "ALTER TABLE tasks ADD COLUMN event_type TEXT NOT NULL DEFAULT 'script';"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
                cur.execute("PRAGMA user_version=2;")
                version = 2
            if version < DB_LATEST_VERSION:
                cur.execute(f"PRAGMA user_version={DB_LATEST_VERSION};")
            self._conn.commit()
        if self.result_retention_per_task > 0:
            deleted = self.prune_all_finished_results()
            if deleted > 0:
                logger.info(
                    "Pruned %s finished task results (retention per task=%s)",
                    deleted,
                    self.result_retention_per_task,
                )

        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT COUNT(1) FROM sqlite_master WHERE type='table' AND name='templates'"
                )
                row = cur.fetchone()
                count = int(row[0]) if row else 0
                if count == 0:
                    # 兼容 ≤ v1.0.7 升级场景；如果 templates 表不存在，创建之（与 _create_schema 中定义一致）
                    cur.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS templates (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            key TEXT NOT NULL UNIQUE,
                            name TEXT NOT NULL,
                            script_body TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                        """
                    )
                    self._conn.commit()
        except Exception:
            logger.exception("Failed to create templates tables")
            pass

    def _create_schema(self, cur: sqlite3.Cursor) -> None:
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                account TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                schedule_expression TEXT,
                condition_script TEXT,
                condition_interval INTEGER NOT NULL DEFAULT 60,
                event_type TEXT NOT NULL DEFAULT 'script',
                is_active INTEGER NOT NULL DEFAULT 1,
                pre_task_ids TEXT NOT NULL DEFAULT '[]',
                script_body TEXT NOT NULL,
                last_run_at TEXT,
                next_run_at TEXT,
                last_condition_check_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                trigger_reason TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                log TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_task_results_task ON task_results(task_id, started_at DESC);
            
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                script_body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Utility methods -----------------------------------------------------
    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["is_active"] = bool(data.get("is_active"))
        data["condition_interval"] = int(data.get("condition_interval", 60))
        data["pre_task_ids"] = json.loads(data.get("pre_task_ids") or "[]")
        data["event_type"] = data.get("event_type") or EVENT_TYPE_SCRIPT
        return data

    # Templates management ----------------------------------------------
    def list_templates(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM templates ORDER BY id ASC")
            rows = [dict(row) for row in cur.fetchall()]
        return rows

    def get_template(self, template_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM templates WHERE id=?", (template_id,)
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def create_template(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = isoformat(time_now())
        key = (payload.get("key") or "").strip()
        name = (payload.get("name") or "").strip()
        script_body = (payload.get("script_body") or "").strip()
        if not name:
            raise ValueError("template name is required")
        if not script_body:
            raise ValueError("template script body is required")
        if not key:
            # 自动生成 key（基于 name）
            base = name.lower().replace(" ", "_")
            key = base
            idx = 1
            while True:
                cur = self._conn.execute(
                    "SELECT COUNT(1) FROM templates WHERE key=?", (key,)
                )
                (count,) = cur.fetchone()
                if count == 0:
                    break
                idx += 1
                key = f"{base}_{idx}"
        now_iso = now
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO templates (key, name, script_body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (key, name, script_body, now_iso, now_iso),
                )
                self._conn.commit()
                tid = cur.lastrowid
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "unique" in msg or "templates.key" in msg:
                    raise ValueError("template key already exists") from exc
                raise ValueError("database integrity error") from exc
        return self.get_template(tid)  # type: ignore

    def update_template(
        self, template_id: int, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_template(template_id)
        if not existing:
            return None
        name = payload.get("name", existing.get("name", "")).strip()
        script_body = payload.get(
            "script_body", existing.get("script_body", "")
        ).strip()
        key = payload.get("key", existing.get("key", "")).strip()
        if not name:
            raise ValueError("template name is required")
        if not script_body:
            raise ValueError("template script body is required")
        updated_at = isoformat(time_now())
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE templates SET key=?, name=?, script_body=?, updated_at=? WHERE id=?",
                    (key, name, script_body, updated_at, template_id),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as exc:
            msg = str(exc).lower()
            if "unique" in msg or "templates.key" in msg:
                raise ValueError("template key already exists") from exc
            raise ValueError("database integrity error") from exc
        return self.get_template(template_id)

    def delete_template(self, template_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def import_templates(self, mapping: Dict[str, Dict[str, str]]) -> Dict[str, int]:
        """Import templates from a mapping like templates.json (key -> {name, script_body}).
        Returns summary counts: inserted, updated"""
        inserted = 0
        updated = 0
        now = isoformat(time_now())
        with self._lock:
            for key, meta in (mapping or {}).items():
                name = (meta.get("name") or key).strip()
                script_body = (meta.get("script_body") or "").strip()
                if not script_body:
                    continue
                cur = self._conn.execute("SELECT id FROM templates WHERE key=?", (key,))
                row = cur.fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE templates SET name=?, script_body=?, updated_at=? WHERE key=?",
                        (name, script_body, now, key),
                    )
                    updated += 1
                else:
                    self._conn.execute(
                        "INSERT INTO templates (key, name, script_body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (key, name, script_body, now, now),
                    )
                    inserted += 1
            self._conn.commit()
        return {"inserted": inserted, "updated": updated}

    def export_templates(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, name, script_body FROM templates ORDER BY id ASC"
            )
            for row in cur.fetchall():
                out[row[0]] = {"name": row[1], "script_body": row[2]}
        return out

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM tasks ORDER BY id ASC")
            rows = [self._row_to_dict(row) for row in cur.fetchall()]
        return rows

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def create_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        now = isoformat(time_now())
        task = self._prepare_task_payload(payload, is_update=False)
        task["created_at"] = now
        task["updated_at"] = now
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO tasks (
                        name, account, trigger_type, schedule_expression, condition_script,
                        condition_interval, event_type, is_active, pre_task_ids, script_body,
                        last_run_at, next_run_at, last_condition_check_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task["name"],
                        task["account"],
                        task["trigger_type"],
                        task.get("schedule_expression"),
                        task.get("condition_script"),
                        task["condition_interval"],
                        task["event_type"],
                        1 if task["is_active"] else 0,
                        json.dumps(task["pre_task_ids"]),
                        task["script_body"],
                        task.get("last_run_at"),
                        task.get("next_run_at"),
                        task.get("last_condition_check_at"),
                        task["created_at"],
                        task["updated_at"],
                    ),
                )
                task_id = cur.lastrowid
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                msg = str(exc).lower()
                if "unique" in msg or "tasks.name" in msg:
                    raise ValueError("task name already exists") from exc
                raise ValueError("database integrity error") from exc
        return self.get_task(task_id)  # type: ignore

    def update_task(
        self, task_id: int, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_task(task_id)
        if not existing:
            return None
        # 检查 Cron 表达式是否变更，变更则强制 next_run_at 重新计算
        old_expr = existing.get("schedule_expression")
        new_expr = payload.get("schedule_expression", old_expr)
        if (
            existing.get("trigger_type") == "schedule"
            and old_expr != new_expr
            and new_expr
        ):
            payload = dict(payload)
            payload["next_run_at"] = None  # 让 _prepare_task_payload 自动计算
        task = self._prepare_task_payload({**existing, **payload}, is_update=True)
        task["updated_at"] = isoformat(time_now())
        try:
            with self._lock:
                self._conn.execute(
                    """
                        UPDATE tasks SET
                            name=?, account=?, trigger_type=?, schedule_expression=?, condition_script=?,
                            condition_interval=?, event_type=?, is_active=?, pre_task_ids=?, script_body=?,
                            last_run_at=?, next_run_at=?, last_condition_check_at=?, updated_at=?
                        WHERE id=?
                        """,
                    (
                        task["name"],
                        task["account"],
                        task["trigger_type"],
                        task.get("schedule_expression"),
                        task.get("condition_script"),
                        task["condition_interval"],
                        task["event_type"],
                        1 if task["is_active"] else 0,
                        json.dumps(task["pre_task_ids"]),
                        task["script_body"],
                        task.get("last_run_at"),
                        task.get("next_run_at"),
                        task.get("last_condition_check_at"),
                        task["updated_at"],
                        task_id,
                    ),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as exc:
            msg = str(exc).lower()
            if "unique" in msg or "tasks.name" in msg:
                raise ValueError("task name already exists") from exc
            raise ValueError("database integrity error") from exc
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def prune_finished_results(self, task_id: int) -> int:
        if self.result_retention_per_task <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                """
                DELETE FROM task_results
                WHERE task_id = ?
                  AND status != 'running'
                  AND id NOT IN (
                      SELECT id FROM task_results
                      WHERE task_id = ?
                        AND status != 'running'
                      ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
                      LIMIT ?
                  )
                """,
                (task_id, task_id, self.result_retention_per_task),
            )
            self._conn.commit()
            return cur.rowcount

    def prune_all_finished_results(self) -> int:
        if self.result_retention_per_task <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute("SELECT id FROM tasks")
            task_ids = [int(row[0]) for row in cur.fetchall()]
        deleted = 0
        for task_id in task_ids:
            deleted += self.prune_finished_results(task_id)
        return deleted

    def record_result_start(self, task_id: int, trigger_reason: str) -> int:
        now = isoformat(time_now())
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO task_results(task_id, status, trigger_reason, started_at)
                VALUES (?, 'running', ?, ?)
                """,
                (task_id, trigger_reason, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def finalize_result(self, result_id: int, status: str, log_text: str) -> None:
        now = isoformat(time_now())
        task_id: Optional[int] = None
        with self._lock:
            cur = self._conn.execute(
                "SELECT task_id FROM task_results WHERE id=?",
                (result_id,),
            )
            row = cur.fetchone()
            task_id = int(row[0]) if row else None
            self._conn.execute(
                "UPDATE task_results SET status=?, finished_at=?, log=? WHERE id=?",
                (status, now, log_text, result_id),
            )
            self._conn.commit()
        if task_id is not None:
            self.prune_finished_results(task_id)

    def fetch_results(
        self, task_id: int, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM task_results WHERE task_id=? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            )
            rows = [dict(row) for row in cur.fetchall()]
        return rows

    def fetch_result(self, task_id: int, result_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM task_results WHERE task_id=? AND id=?",
                (task_id, result_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def delete_results(self, task_id: int, result_id: Optional[int] = None) -> int:
        with self._lock:
            if result_id is None:
                cur = self._conn.execute(
                    "DELETE FROM task_results WHERE task_id=?", (task_id,)
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM task_results WHERE task_id=? AND id=?",
                    (task_id, result_id),
                )
            self._conn.commit()
            return cur.rowcount

    def get_latest_result(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM task_results WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def has_running_instance(self, task_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(1) FROM task_results WHERE task_id=? AND status='running'",
                (task_id,),
            )
            (count,) = cur.fetchone()
        return count > 0

    def finalize_stale_running_instances(
        self, task_id: int, reason: str = "stopped by user"
    ) -> int:
        now = isoformat(time_now())
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE task_results
                SET status='failed',
                    finished_at=?,
                    log=CASE
                        WHEN log IS NULL OR log='' THEN ?
                        ELSE log || '\n' || ?
                    END
                WHERE task_id=? AND status='running'
                """,
                (now, reason, reason, task_id),
            )
            self._conn.commit()
            return cur.rowcount

    def update_last_run(self, task_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET last_run_at=?, updated_at=? WHERE id=?",
                (isoformat(time_now()), isoformat(time_now()), task_id),
            )
            self._conn.commit()

    def schedule_next_run(
        self, task_id: int, expression: str, base: Optional[datetime] = None
    ) -> Optional[str]:
        if not expression:
            return None
        cron = CronExpression(expression)
        next_dt = cron.next_after(base or time_now())
        next_iso = isoformat(next_dt)
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET next_run_at=?, updated_at=? WHERE id=?",
                (next_iso, isoformat(time_now()), task_id),
            )
            self._conn.commit()
        return next_iso

    def update_condition_check(self, task_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET last_condition_check_at=?, updated_at=? WHERE id=?",
                (isoformat(time_now()), isoformat(time_now()), task_id),
            )
            self._conn.commit()

    def fetch_due_tasks(self, moment: datetime) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE trigger_type='schedule' AND is_active=1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (isoformat(moment),),
            )
            rows = [self._row_to_dict(row) for row in cur.fetchall()]
        return rows

    def fetch_event_tasks(
        self, event_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM tasks WHERE trigger_type='event' AND is_active=1"
        params: List[Any] = []
        if event_type:
            query += " AND event_type=?"
            params.append(event_type)
        query += " ORDER BY id ASC"
        with self._lock:
            cur = self._conn.execute(query, params)
            rows = [self._row_to_dict(row) for row in cur.fetchall()]
        return rows

    # Payload utilities ---------------------------------------------------
    def _prepare_task_payload(
        self, payload: Dict[str, Any], is_update: bool
    ) -> Dict[str, Any]:
        trigger_type = payload.get("trigger_type", "schedule")
        if trigger_type not in {"schedule", "event"}:
            raise ValueError("trigger_type must be 'schedule' or 'event'")
        name = payload.get("name", "").strip()
        account_raw = payload.get("account", "")
        account = account_raw.strip()
        if not account and not POSIX_ACCOUNT_SUPPORT:
            account = DEFAULT_ACCOUNT_NAME
        if not name:
            raise ValueError("task name is required")
        if not account:
            raise ValueError("account is required")
        account = ensure_account_allowed(account)
        script_body = payload.get("script_body", "").strip()
        if not script_body:
            raise ValueError("script body is required")

        is_active = bool(payload.get("is_active", True))
        schedule_expression_raw = payload.get("schedule_expression")
        schedule_expression = (
            schedule_expression_raw.strip()
            if isinstance(schedule_expression_raw, str)
            else schedule_expression_raw
        )
        condition_script_raw = payload.get("condition_script")
        condition_script = (
            condition_script_raw.strip()
            if isinstance(condition_script_raw, str)
            else condition_script_raw
        )
        condition_interval = max(10, int(payload.get("condition_interval", 60)))
        event_type_raw = payload.get("event_type")
        event_type = (
            (event_type_raw or EVENT_TYPE_SCRIPT).strip()
            if isinstance(event_type_raw, str)
            else (event_type_raw or EVENT_TYPE_SCRIPT)
        )
        pre_task_ids = payload.get("pre_task_ids") or []
        if isinstance(pre_task_ids, str):
            try:
                pre_task_ids = json.loads(pre_task_ids)
            except json.JSONDecodeError as exc:
                raise ValueError("pre_task_ids format error") from exc
        current_id = payload.get("id")
        if current_id is not None:
            current_id = int(current_id)
        cleaned: List[int] = []
        for tid in pre_task_ids:
            tid_int = int(tid)
            if current_id is not None and tid_int == current_id:
                continue
            if tid_int not in cleaned:
                cleaned.append(tid_int)
        pre_task_ids = cleaned

        next_run_at: Optional[str] = payload.get("next_run_at")
        last_condition_check_at = payload.get("last_condition_check_at")

        if trigger_type == "schedule":
            if not schedule_expression:
                raise ValueError("schedule expression is required")
            cron = CronExpression(schedule_expression)
            if not is_update or not next_run_at:
                next_run_at = isoformat(cron.next_after(time_now()))
            condition_script = None
            event_type = EVENT_TYPE_SCRIPT
        else:
            if event_type not in EVENT_TYPES:
                raise ValueError("event type is not supported")
            if event_type == EVENT_TYPE_SCRIPT:
                if not condition_script:
                    raise ValueError("event tasks require condition script")
                last_condition_check_at = payload.get("last_condition_check_at")
            else:
                condition_script = None
                last_condition_check_at = None
            schedule_expression = None

        return {
            "name": name,
            "account": account,
            "trigger_type": trigger_type,
            "schedule_expression": schedule_expression,
            "condition_script": condition_script,
            "condition_interval": condition_interval,
            "event_type": event_type,
            "is_active": is_active,
            "pre_task_ids": pre_task_ids,
            "script_body": script_body,
            "last_run_at": payload.get("last_run_at"),
            "next_run_at": next_run_at,
            "last_condition_check_at": last_condition_check_at,
        }


###############################################################################
# Scheduler engine
###############################################################################


class TaskRunner(threading.Thread):
    _running_lock = threading.RLock()
    _running_processes: Dict[int, Set[Popen[str]]] = {}

    def __init__(
        self,
        db: Database,
        task: Dict[str, Any],
        trigger_reason: str,
        settings: SchedulerSettings,
    ):
        super().__init__(daemon=True)
        self.db = db
        self.task = task
        self.trigger_reason = trigger_reason
        self.settings = settings

    def run(self) -> None:
        task_id = self.task["id"]
        logger.info("Executing task %s (%s)", task_id, self.trigger_reason)
        result_id = self.db.record_result_start(task_id, self.trigger_reason)
        execution_timeout = self.settings.task_timeout or None
        try:
            log_text, status = self._execute_script(
                self.task["script_body"], execution_timeout
            )
        except Exception as exc:  # pylint: disable=broad-except
            status = "failed"
            log_text = f"task execution exception: {exc!r}"
        finally:
            self.db.finalize_result(result_id, status, log_text)
            self.db.update_last_run(task_id)

    def _execute_script(self, script: str, timeout: Optional[int]) -> tuple[str, str]:
        cmd = self._build_command(script)
        env = os.environ.copy()
        preexec_fn, home_dir = self._prepare_account_context()
        task_id = int(self.task["id"])
        if home_dir:
            env["HOME"] = home_dir
        env.update(
            {
                "SCHEDULER_TASK_ID": str(self.task["id"]),
                "SCHEDULER_TASK_NAME": self.task["name"],
                "SCHEDULER_TASK_ACCOUNT": self.task["account"],
                "SCHEDULER_TRIGGER": self.trigger_reason,
            }
        )
        try:
            process: Popen[str] = Popen(
                cmd,
                stdout=PIPE,
                stderr=PIPE,
                text=True,
                env=env,
                preexec_fn=preexec_fn,
            )
            self._register_process(task_id, process)
            stdout, stderr = process.communicate(timeout=timeout)
        except TimeoutExpired as exc:
            try:
                process.kill()  # type: ignore[name-defined]
                process.communicate()
            except Exception:
                pass
            return f"task execution timeout (> {timeout}s): {exc}", "failed"
        except Exception as exc:  # pylint: disable=broad-except
            return str(exc), "failed"
        finally:
            proc = locals().get("process")
            if proc is not None:
                self._unregister_process(task_id, proc)
        output = (stdout or "") + (stderr or "")
        status = "success" if process.returncode == 0 else "failed"
        return output.strip(), status

    @classmethod
    def _register_process(cls, task_id: int, process: Popen[str]) -> None:
        with cls._running_lock:
            cls._running_processes.setdefault(task_id, set()).add(process)

    @classmethod
    def _unregister_process(cls, task_id: int, process: Popen[str]) -> None:
        with cls._running_lock:
            processes = cls._running_processes.get(task_id)
            if not processes:
                return
            processes.discard(process)
            if not processes:
                cls._running_processes.pop(task_id, None)

    @classmethod
    def terminate_task_processes(
        cls, task_id: int, grace_seconds: float = 3.0
    ) -> Dict[str, int]:
        with cls._running_lock:
            processes = list(cls._running_processes.get(task_id, set()))

        fallback_pids: List[int] = []
        if not processes:
            fallback_pids = cls._find_task_pids(task_id)
            if not fallback_pids:
                return {
                    "targeted": 0,
                    "terminated": 0,
                    "killed": 0,
                    "already_exited": 0,
                }
            return cls._terminate_pids(fallback_pids, grace_seconds)

        terminated = 0
        killed = 0
        already_exited = 0

        alive_after_terminate: List[Popen[str]] = []
        for process in processes:
            if process.poll() is not None:
                already_exited += 1
                continue
            try:
                process.terminate()
                terminated += 1
                alive_after_terminate.append(process)
            except Exception:
                if process.poll() is not None:
                    already_exited += 1

        deadline = time.monotonic() + max(0.0, grace_seconds)
        survivors: List[Popen[str]] = []
        for process in alive_after_terminate:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    process.wait(timeout=remaining)
                except TimeoutExpired:
                    pass
            if process.poll() is None:
                survivors.append(process)

        for process in survivors:
            try:
                process.kill()
                killed += 1
            except Exception:
                pass

        return {
            "targeted": len(processes),
            "terminated": terminated,
            "killed": killed,
            "already_exited": already_exited,
        }

    @staticmethod
    def _find_task_pids(task_id: int) -> List[int]:
        if os.name != "posix":
            return []
        target = f"SCHEDULER_TASK_ID={task_id}".encode("utf-8")
        current_pid = os.getpid()
        found: Set[int] = set()
        proc_root = "/proc"
        try:
            entries = os.listdir(proc_root)
        except Exception:
            return []

        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == current_pid:
                continue
            env_path = os.path.join(proc_root, entry, "environ")
            try:
                with open(env_path, "rb") as env_file:
                    content = env_file.read()
            except Exception:
                continue
            if target in content:
                found.add(pid)
        return sorted(found)

    @staticmethod
    def _terminate_pids(pids: List[int], grace_seconds: float = 3.0) -> Dict[str, int]:
        terminated = 0
        killed = 0
        already_exited = 0
        alive_after_terminate: List[int] = []

        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                terminated += 1
                alive_after_terminate.append(pid)
            except ProcessLookupError:
                already_exited += 1
            except PermissionError:
                continue

        deadline = time.monotonic() + max(0.0, grace_seconds)
        survivors: List[int] = []
        for pid in alive_after_terminate:
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    break
                time.sleep(0.1)
            else:
                survivors.append(pid)

        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                already_exited += 1
            except PermissionError:
                continue

        return {
            "targeted": len(pids),
            "terminated": terminated,
            "killed": killed,
            "already_exited": already_exited,
        }

    @staticmethod
    def _build_command(script: str) -> List[str]:
        if os.name == "nt":
            return [
                "powershell",
                "-NoLogo",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ]
        return ["/bin/bash", "-c", script]

    def _prepare_account_context(
        self,
    ) -> tuple[Optional[Callable[[], None]], Optional[str]]:
        if not POSIX_ACCOUNT_SUPPORT:
            return (None, None)
        account = self.task.get("account")
        if not account:
            return (None, None)
        try:
            pw_record = pwd.getpwnam(account)  # type: ignore[attr-defined]
        except KeyError as exc:
            raise RuntimeError(
                f"account {account} does not exist, cannot execute task"
            ) from exc

        target_uid = pw_record.pw_uid
        target_gid = pw_record.pw_gid
        current_uid = os.geteuid()

        if current_uid == target_uid:
            return (None, pw_record.pw_dir)

        if current_uid != 0:
            raise PermissionError(
                "scheduler service must run as root to switch task execution account"
            )

        supplemental: List[int] = []
        try:
            supplemental = [entry.gr_gid for entry in grp.getgrall() if account in entry.gr_mem]  # type: ignore[attr-defined]
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "failed to get supplemental groups for account %s: %s", account, exc
            )

        groups = sorted(set([target_gid, *supplemental]))

        def _changer() -> None:
            os.setgid(target_gid)
            if groups:
                os.setgroups(groups)
            os.setuid(target_uid)

        return (_changer, pw_record.pw_dir)


class SchedulerEngine:
    def __init__(self, db: Database, settings: SchedulerSettings):
        self.db = db
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        # 记录服务启动时间，用于跳过重启前已过期的定时任务
        self.started_at: Optional[datetime] = None

    def start(self) -> None:
        # 标记启动时刻，之后复核过期任务时会基于此时间跳过历史遗留的执行
        self.started_at = time_now()
        self.thread.start()
        self._trigger_system_event(EVENT_TYPE_BOOT)

    def stop(self) -> None:
        self.stop_event.set()
        self._trigger_system_event(EVENT_TYPE_SHUTDOWN)
        self.thread.join(timeout=5)

    # Internal ------------------------------------------------------------
    def _loop(self) -> None:
        while not self.stop_event.is_set():
            now = time_now()
            try:
                self._process_due_tasks(now)
                self._process_event_tasks(now)
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Scheduler loop error: %s", exc)
            self.stop_event.wait(1)

    def _process_due_tasks(self, moment: datetime) -> None:
        for task in self.db.fetch_due_tasks(moment):
            # 跳过那些在服务启动之前就已经过期的任务（避免重启后回放执行）
            try:
                next_run_dt = parse_iso(task.get("next_run_at"))
            except Exception:
                next_run_dt = None
            if self.started_at and next_run_dt and next_run_dt < self.started_at:
                logger.info(
                    "Skipping expired task %s scheduled at %s (service started at %s)",
                    task.get("id"),
                    task.get("next_run_at"),
                    isoformat(self.started_at),
                )
                # 重新安排到下一个可用时间，但不执行错过的运行
                try:
                    self.db.schedule_next_run(
                        task["id"], task["schedule_expression"], self.started_at
                    )
                except Exception:
                    logger.exception(
                        "Failed to reschedule expired task %s", task.get("id")
                    )
                continue
            if self.db.has_running_instance(task["id"]):
                logger.info("Task %s still running, skip", task["id"])
                continue
            if not self._dependencies_met(task):
                logger.info("Task %s waiting for dependencies", task["id"])
                # re-schedule shortly in future to retry
                self.db.schedule_next_run(
                    task["id"],
                    task["schedule_expression"],
                    moment + timedelta(minutes=1),
                )
                continue
            TaskRunner(self.db, task, "schedule", self.settings).start()
            self.db.schedule_next_run(task["id"], task["schedule_expression"], moment)

    def _process_event_tasks(self, moment: datetime) -> None:
        for task in self.db.fetch_event_tasks(event_type=EVENT_TYPE_SCRIPT):
            last_check = parse_iso(task.get("last_condition_check_at"))
            interval = task.get("condition_interval", 60)
            if last_check and (moment - last_check).total_seconds() < interval:
                continue
            self.db.update_condition_check(task["id"])
            if not task.get("condition_script"):
                continue
            ok = self._run_condition(task)
            if not ok:
                continue
            if self.db.has_running_instance(task["id"]):
                continue
            if not self._dependencies_met(task):
                continue
            TaskRunner(self.db, task, "condition", self.settings).start()

    def _run_condition(self, task: Dict[str, Any]) -> bool:
        command = TaskRunner._build_command(task["condition_script"])
        try:
            completed = run(
                command,
                capture_output=True,
                text=True,
                timeout=self.settings.condition_timeout,
                check=False,
            )
        except TimeoutExpired as exc:
            logger.warning("Condition script timeout for task %s: %s", task["id"], exc)
            return False
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Condition script for task %s failed: %s", task["id"], exc)
            return False
        if completed.returncode != 0:
            return False
        return True

    def _dependencies_met(self, task: Dict[str, Any]) -> bool:
        deps = task.get("pre_task_ids") or []
        for dep_id in deps:
            result = self.db.get_latest_result(dep_id)
            if not result or result.get("status") != "success":
                return False
        return True

    def _trigger_system_event(self, event_type: str) -> None:
        if event_type not in {EVENT_TYPE_BOOT, EVENT_TYPE_SHUTDOWN}:
            return
        trigger_reason = (
            "system_boot" if event_type == EVENT_TYPE_BOOT else "system_shutdown"
        )
        runners: List[TaskRunner] = []
        for task in self.db.fetch_event_tasks(event_type=event_type):
            if self.db.has_running_instance(task["id"]):
                continue
            if not self._dependencies_met(task):
                continue
            runner = TaskRunner(self.db, task, trigger_reason, self.settings)
            runner.start()
            runners.append(runner)
        for runner in runners:
            runner.join()


###############################################################################
# HTTP layer
###############################################################################


class SchedulerContext:
    def __init__(self, db: Database, engine: SchedulerEngine, settings: SchedulerSettings):
        self.db = db
        self.engine = engine
        self.settings = settings


class SchedulerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    block_on_close = False

    def __init__(
        self,
        server_address,
        handler_class,
        *,
        base_path: str = "/",
        prefer_ipv6: bool = False,
        unix_socket_path: Optional[str] = None,
        bind_and_activate: bool = True,
    ):
        host = server_address[0] if server_address else ""
        port = server_address[1] if len(server_address) > 1 else 0

        self.base_path = base_path or "/"

        # If unix_socket_path is provided, create a UNIX domain socket and
        # initialize the HTTP server without binding/activating the default
        # TCP socket. Otherwise, behave as normal TCP server.
        if unix_socket_path:
            # Initialize without binding so we can replace the socket.
            super().__init__(("", 0), handler_class, bind_and_activate=False)
            # ensure old socket file removed
            try:
                if os.path.exists(unix_socket_path):
                    os.unlink(unix_socket_path)
            except Exception:
                pass
            uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            uds.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            uds.bind(unix_socket_path)
            # let HTTPServer.server_activate perform the listen
            self.socket = uds
            self.address_family = socket.AF_UNIX
            # set a human-readable server_address
            self.server_address = unix_socket_path
            # activate server (calls listen)
            self.server_activate()
        else:
            use_ipv6 = False
            if (prefer_ipv6 or (host and ":" in host)) and socket.has_ipv6:
                use_ipv6 = True
            elif (prefer_ipv6 or (host and ":" in host)) and not socket.has_ipv6:
                raise RuntimeError("current system does not support IPv6 listening")
            if use_ipv6:
                self.address_family = socket.AF_INET6
                if len(server_address) == 2:
                    server_address = (host, port, 0, 0)
            super().__init__(
                server_address, handler_class, bind_and_activate=bind_and_activate
            )
            if use_ipv6:
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass


class SchedulerRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if not self._ensure_base_path():
            return
        if self.path.startswith("/api/"):
            self._handle_api("GET")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unsupported path")

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if not self._ensure_base_path():
            return
        if self.path.startswith("/api/"):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "HEAD not supported for API")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unsupported path")

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if not self._ensure_base_path():
            return
        if self.path.startswith("/api/"):
            self._handle_api("POST")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unsupported path")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if not self._ensure_base_path():
            return
        if self.path.startswith("/api/"):
            self._handle_api("PUT")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unsupported path")

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._require_auth():
            return
        if not self._ensure_base_path():
            return
        if self.path.startswith("/api/"):
            self._handle_api("DELETE")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unsupported path")

    # API routing ---------------------------------------------------------
    def _handle_api(self, method: str) -> None:
        parsed = urlparse(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment][
            1:
        ]  # drop 'api'
        try:
            if not segments:
                self._json_response({"message": "scheduler api"})
                return
            resource = segments[0]
            if resource == "health" and method == "GET":
                self._health()
                return
            if resource == "accounts" and method == "GET":
                self._list_accounts()
                return
            if resource == "settings":
                if method == "GET":
                    self._get_settings()
                    return
                if method == "PUT":
                    self._update_settings()
                    return
            if resource == "templates":
                self._handle_templates(method, segments[1:])
                return
            if resource == "fs":
                # server-side filesystem browsing and reading
                self._handle_fs(method, segments[1:])
                return
            if resource == "tasks":
                self._handle_tasks(method, segments[1:])
                return
            if resource == "results" and len(segments) >= 2:
                task_id = int(segments[1])
                if len(segments) == 2 and method == "GET":
                    self._list_results(task_id)
                    return
            self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("API error: %s", exc)
            self._json_response(
                {"error": "internal server error"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _list_accounts(self) -> None:
        payload = {
            "data": list_allowed_accounts(),
            "meta": {
                "posix_supported": POSIX_ACCOUNT_SUPPORT,
                "default_account": DEFAULT_ACCOUNT_NAME,
            },
        }
        self._json_response(payload)

    def _get_settings(self) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        self._json_response({"data": ctx.settings.to_dict()})

    def _update_settings(self) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        payload = self._read_json() or {}
        updated = ctx.settings.update(payload)
        ctx.db.result_retention_per_task = updated["result_retention_per_task"]
        pruned = ctx.db.prune_all_finished_results()
        self._json_response({"data": updated, "pruned": pruned})

    def _handle_tasks(self, method: str, remainder: List[str]) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        if method == "GET" and not remainder:
            tasks = ctx.db.list_tasks()
            for task in tasks:
                task["latest_result"] = ctx.db.get_latest_result(task["id"])
            self._json_response({"data": tasks})
            return
        if remainder and remainder[0] == "batch":
            if method != "POST":
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
                return
            payload = self._read_json()
            if payload is None:
                return
            self._batch_tasks(payload)
            return
        if not remainder:
            if method == "POST":
                payload = self._read_json()
                if payload is None:
                    return
                try:
                    task = ctx.db.create_task(payload)
                except sqlite3.IntegrityError as exc:
                    # Convert DB constraint errors (e.g. unique name) to client 400
                    logger.info("create_task integrity error: %s", exc)
                    self._json_response(
                        {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST
                    )
                    return
                self._json_response(task, status=HTTPStatus.CREATED)
                return
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        task_id = int(remainder[0])
        if len(remainder) == 1:
            if method == "GET":
                task = ctx.db.get_task(task_id)
                if not task:
                    self.send_error(HTTPStatus.NOT_FOUND, "Task not found")
                    return
                task["latest_result"] = ctx.db.get_latest_result(task_id)
                self._json_response(task)
                return
            if method == "PUT":
                payload = self._read_json()
                if payload is None:
                    return
                try:
                    task = ctx.db.update_task(task_id, payload)
                except sqlite3.IntegrityError as exc:
                    logger.info("update_task integrity error: %s", exc)
                    self._json_response(
                        {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST
                    )
                    return
                if not task:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._json_response(task)
                return
            if method == "DELETE":
                deleted = ctx.db.delete_task(task_id)
                if not deleted:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._json_response({"deleted": True})
                return
        if len(remainder) >= 2:
            action = remainder[1]
            if action == "run" and method == "POST":
                self._run_task(task_id)
                return
            if action == "stop" and method == "POST":
                self._stop_task(task_id)
                return
            if action == "toggle" and method == "POST":
                payload = self._read_json() or {}
                self._toggle_task(task_id, payload)
                return
            if action == "results":
                if method == "GET":
                    if len(remainder) == 2:
                        self._list_results(task_id)
                        return
                    if len(remainder) == 3:
                        result_id = int(remainder[2])
                        self._get_result(task_id, result_id)
                        return
                if method == "DELETE":
                    result_id = int(remainder[2]) if len(remainder) == 3 else None
                    deleted = ctx.db.delete_results(task_id, result_id)
                    self._json_response({"deleted": deleted})
                    return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_templates(self, method: str, remainder: List[str]) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        # 支持：GET /api/templates (list), GET /api/templates/export (export as mapping),
        # POST /api/templates/import (import mapping), POST /api/templates (create),
        # GET/PUT/DELETE /api/templates/{id}
        if method == "GET" and not remainder:
            templates = ctx.db.list_templates()
            self._json_response({"data": templates})
            return
        if remainder and remainder[0] == "export" and method == "GET":
            mapping = ctx.db.export_templates()
            # 返回为原生对象，保持与 templates.json 兼容
            self._json_response(mapping)
            return
        if remainder and remainder[0] == "import" and method == "POST":
            payload = self._read_json()
            if payload is None:
                return
            # 支持直接上传 mapping 对象
            if not isinstance(payload, dict):
                self._json_response(
                    {"error": "import data should be an object mapping"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            invalid_keys = []
            for k, v in payload.items():
                if not isinstance(v, dict):
                    invalid_keys.append(k)
                    continue
                # 必须包含 script_body 字段且为字符串
                if not isinstance(v.get("script_body"), str) or not v.get(
                    "script_body"
                ):
                    invalid_keys.append(k)
            if invalid_keys:
                self._json_response(
                    {"error": "invalid template entries", "invalid_keys": invalid_keys},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            summary = ctx.db.import_templates(payload)
            self._json_response({"imported": summary})
            return
        if not remainder:
            if method == "POST":
                payload = self._read_json()
                if payload is None:
                    return
                tpl = ctx.db.create_template(payload)
                self._json_response(tpl, status=HTTPStatus.CREATED)
                return
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        # 处理 /api/templates/{id}
        try:
            tpl_id = int(remainder[0])
        except Exception:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if len(remainder) == 1:
            if method == "GET":
                tpl = ctx.db.get_template(tpl_id)
                if not tpl:
                    self.send_error(HTTPStatus.NOT_FOUND, "Template not found")
                    return
                self._json_response(tpl)
                return
            if method == "PUT":
                payload = self._read_json()
                if payload is None:
                    return
                tpl = ctx.db.update_template(tpl_id, payload)
                if not tpl:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._json_response(tpl)
                return
            if method == "DELETE":
                deleted = ctx.db.delete_template(tpl_id)
                if not deleted:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._json_response({"deleted": True})
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _batch_tasks(self, payload: Dict[str, Any]) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        action = (payload.get("action") or "").strip().lower()
        task_ids_payload = payload.get("task_ids")
        if not isinstance(task_ids_payload, list) or not task_ids_payload:
            raise ValueError("task_ids cannot be empty")
        task_ids = []
        for raw in task_ids_payload:
            try:
                tid = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("task_ids must contain valid task ids") from exc
            if tid > 0 and tid not in task_ids:
                task_ids.append(tid)
        if not task_ids:
            raise ValueError("task_ids must contain valid task ids")

        if action not in {"delete", "enable", "disable", "run", "stop"}:
            raise ValueError("action is not supported")

        result: Dict[str, List[int]] = {"missing": []}
        runners: List[TaskRunner] = []

        for task_id in task_ids:
            task = ctx.db.get_task(task_id)
            if not task:
                result.setdefault("missing", []).append(task_id)
                continue

            if action == "delete":
                if ctx.db.delete_task(task_id):
                    result.setdefault("deleted", []).append(task_id)
                else:
                    result.setdefault("missing", []).append(task_id)
                continue

            if action in {"enable", "disable"}:
                target_state = action == "enable"
                if bool(task["is_active"]) == target_state:
                    result.setdefault("unchanged", []).append(task_id)
                    continue
                ctx.db.update_task(task_id, {"is_active": target_state})
                result.setdefault("updated", []).append(task_id)
                continue

            if action == "run":
                if ctx.db.has_running_instance(task_id):
                    result.setdefault("running", []).append(task_id)
                    continue
                if not ctx.engine._dependencies_met(
                    task
                ):  # pylint: disable=protected-access
                    result.setdefault("blocked", []).append(task_id)
                    continue
                runner = TaskRunner(ctx.db, task, "manual", ctx.settings)
                runner.start()
                runners.append(runner)
                result.setdefault("queued", []).append(task_id)
                continue

            if action == "stop":
                summary = TaskRunner.terminate_task_processes(task_id)
                if summary["targeted"] > 0 and (
                    summary["terminated"] > 0 or summary["killed"] > 0
                ):
                    result.setdefault("stopped", []).append(task_id)
                else:
                    stale_cleared = 0
                    if ctx.db.has_running_instance(task_id):
                        stale_cleared = ctx.db.finalize_stale_running_instances(
                            task_id,
                            reason="stopped by user (no live process found)",
                        )
                    if stale_cleared > 0:
                        result.setdefault("stopped", []).append(task_id)
                    else:
                        result.setdefault("not_running", []).append(task_id)
                continue

        payload = {"action": action, "result": result}
        self._json_response(payload)

    def _run_task(self, task_id: int) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        task = ctx.db.get_task(task_id)
        if not task:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if ctx.db.has_running_instance(task_id):
            self._json_response(
                {"error": "task is running"}, status=HTTPStatus.CONFLICT
            )
            return
        if not ctx.engine._dependencies_met(task):  # pylint: disable=protected-access
            self._json_response(
                {"error": "dependencies are not met"}, status=HTTPStatus.BAD_REQUEST
            )
            return
        TaskRunner(ctx.db, task, "manual", ctx.settings).start()
        self._json_response({"queued": True})

    def _stop_task(self, task_id: int) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        task = ctx.db.get_task(task_id)
        if not task:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        summary = TaskRunner.terminate_task_processes(task_id)
        stopped = summary["targeted"] > 0 and (
            summary["terminated"] > 0 or summary["killed"] > 0
        )
        if not stopped and ctx.db.has_running_instance(task_id):
            stale_cleared = ctx.db.finalize_stale_running_instances(
                task_id,
                reason="stopped by user (no live process found)",
            )
            stopped = stale_cleared > 0
        if not stopped:
            self._json_response(
                {"stopped": False, "reason": "not_running", "summary": summary},
                status=HTTPStatus.CONFLICT,
            )
            return
        self._json_response({"stopped": True, "summary": summary})

    def _toggle_task(self, task_id: int, payload: Dict[str, Any]) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        task = ctx.db.get_task(task_id)
        if not task:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        is_active = bool(payload.get("is_active", not task["is_active"]))
        updated = ctx.db.update_task(task_id, {"is_active": is_active})
        self._json_response(updated)

    def _list_results(self, task_id: int) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        query = parse_qs(urlparse(self.path).query)
        limit = int(query.get("limit", [50])[0])
        offset = int(query.get("offset", [0])[0])
        summary_mode = query.get("summary", ["0"])[0].lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        log_limit = int(
            query.get("log_limit", [ctx.settings.result_log_preview_limit])[0]
        )
        results = ctx.db.fetch_results(task_id, limit=limit, offset=offset)
        payload = [
            serialize_result_row(
                row,
                include_log=not summary_mode,
                log_limit=log_limit if summary_mode else None,
            )
            for row in results
        ]
        self._json_response({"data": payload})

    def _get_result(self, task_id: int, result_id: int) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        result = ctx.db.fetch_result(task_id, result_id)
        if not result:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._json_response({"data": serialize_result_row(result, include_log=True)})

    def _handle_fs(self, method: str, remainder: List[str]) -> None:
        # Support: GET /api/fs/list?path=... , GET /api/fs/read?path=... and POST /api/fs/write?path=...
        # determine action from path segment first so we can allow POST for 'write'
        action = remainder[0] if remainder else "list"
        if action == "write" and method != "POST":
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if action in ("list", "read") and method != "GET":
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        query = parse_qs(urlparse(self.path).query)
        query_path = query.get("path", [None])[0]
        header_path = None
        try:
            header_path = self.headers.get("X-FS-Path")
        except Exception:
            header_path = None
        # prefer explicit header (proxy-friendly), then query, else try path-in-segment, finally default '/'
        path = (
            header_path
            if header_path is not None
            else (query_path if query_path is not None else None)
        )
        if path is None:
            # check if client encoded the desired path as the next path segment: /api/fs/list/%2Ftmp
            try:
                if remainder and len(remainder) > 1:
                    seg = remainder[1]
                    if seg:
                        path = unquote(seg)
            except Exception:
                path = None
        if path is None:
            path = "/"
        # normalize input path
        try:
            # allow absolute paths; otherwise treat relative to server root
            if not path:
                path = "/"
            if not os.path.isabs(path):
                target = os.path.normpath(os.path.join(ROOT_DIR, path))
            else:
                target = os.path.normpath(path)
        except Exception:
            self._json_response(
                {"error": "invalid path"}, status=HTTPStatus.BAD_REQUEST
            )
            return

        # Log incoming request path, parsed query/header path and resolved filesystem target
        try:
            logger.info(
                "_handle_fs request: raw_path=%s, query_path=%s, header_path=%s, resolved_target=%s",
                self.path,
                query_path,
                header_path,
                target,
            )
        except Exception:
            pass

        if action == "list":
            self._list_fs(target)
            return
        if action == "read":
            self._read_fs(target)
            return
        if action == "write":
            self._write_fs(target)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _list_fs(self, target: str) -> None:
        # Return JSON listing for directory
        if not os.path.exists(target):
            self.send_error(HTTPStatus.NOT_FOUND, "Path not found")
            return
        if not os.path.isdir(target):
            self.send_error(HTTPStatus.BAD_REQUEST, "Not a directory")
            return
        try:
            entries = []
            with os.scandir(target) as it:
                for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
                    entries.append(
                        {
                            "name": entry.name,
                            "path": os.path.join(target, entry.name),
                            "isdir": entry.is_dir(),
                        }
                    )
            self._json_response({"files": entries})
        except PermissionError:
            self._json_response(
                {"error": "permission denied"}, status=HTTPStatus.FORBIDDEN
            )
        except Exception as exc:
            logger.exception("_list_fs error: %s", exc)
            self._json_response(
                {"error": "internal error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def _read_fs(self, target: str) -> None:
        # Return file content as plain text
        if not os.path.exists(target):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        if not os.path.isfile(target):
            self.send_error(HTTPStatus.BAD_REQUEST, "Not a file")
            return
        try:
            # attempt to read as text
            with open(target, "rb") as fh:
                data = fh.read()
            # Try to decode as UTF-8, fall back to latin-1 to avoid decode errors
            try:
                text = data.decode("utf-8")
            except Exception:
                text = data.decode("latin-1")
            body = text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except PermissionError:
            self.send_error(HTTPStatus.FORBIDDEN, "Permission denied")
        except Exception as exc:
            logger.exception("_read_fs error: %s", exc)
            self._json_response(
                {"error": "internal error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def _write_fs(self, target: str) -> None:
        # Write provided content (JSON body {"content": "..."}) to target path
        try:
            payload = self._read_json()
            if payload is None:
                return
            if not isinstance(payload, dict) or "content" not in payload:
                self._json_response(
                    {"error": "missing content"}, status=HTTPStatus.BAD_REQUEST
                )
                return
            content = payload.get("content", "")
            if not isinstance(content, str):
                self._json_response(
                    {"error": "content must be a string"}, status=HTTPStatus.BAD_REQUEST
                )
                return
            parent = os.path.dirname(target) or "/"
            # Ensure parent directory exists (try to create)
            if not os.path.exists(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except Exception:
                    self._json_response(
                        {"error": "parent directory missing and cannot be created"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
            # write file (utf-8)
            try:
                with open(target, "wb") as fh:
                    fh.write(content.encode("utf-8"))
                self._json_response({"written": True, "path": target})
            except PermissionError:
                self._json_response(
                    {"error": "permission denied"}, status=HTTPStatus.FORBIDDEN
                )
            except Exception as exc:
                logger.exception("_write_fs error: %s", exc)
                self._json_response(
                    {"error": "internal error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR
                )
        except Exception as exc:
            logger.exception("_write_fs top-level error: %s", exc)
            self._json_response(
                {"error": "internal error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR
            )

    def _health(self) -> None:
        ctx: SchedulerContext = self.server.app_context  # type: ignore[attr-defined]
        tasks = ctx.db.list_tasks()
        payload = {
            "time": isoformat(time_now()),
            "task_count": len(tasks),
        }
        self._json_response(payload)

    # Utilities -----------------------------------------------------------
    def _read_json(self) -> Optional[Dict[str, Any]]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json_response(
                {"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST
            )
            return None
        if not isinstance(payload, dict):
            self._json_response(
                {"error": "JSON body must be an object"}, status=HTTPStatus.BAD_REQUEST
            )
            return None
        return payload

    def _json_response(
        self, payload: Any, status: HTTPStatus | int = HTTPStatus.OK
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_: str, *args: Any) -> None:  # noqa: D401
        if not LOG_POLLING_REQUESTS:
            request_line = str(args[0]) if args else ""
            try:
                status_code = int(args[1]) if len(args) > 1 else 0
            except (TypeError, ValueError):
                status_code = 0
            request_path = urlsplit(self.path).path if getattr(self, "path", "") else ""
            request_method = getattr(self, "command", "")
            if (
                (
                    request_path in QUIET_ACCESS_LOG_PATHS
                    or request_line.startswith("GET /api/tasks ")
                    or request_line.startswith("HEAD /api/tasks ")
                    or request_line.startswith("GET /api/health ")
                    or request_line.startswith("HEAD /api/health ")
                    or request_line.startswith("GET /api/tasks/version ")
                    or request_line.startswith("HEAD /api/tasks/version ")
                )
                and request_method in {"GET", "HEAD", ""}
                and 200 <= status_code < 400
            ):
                return
        ca = getattr(self, "client_address", None)
        if isinstance(ca, (list, tuple)) and ca:
            addr = ca[0]
        else:
            addr = ca or "-"
        logger.info("%s - - %s", addr, format_ % args)

    def _require_auth(self) -> bool:
        # Authentication handled by front-end/proxy; backend accepts requests.
        return True

    def _send_auth_challenge(self, realm: str) -> None:
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", f'Basic realm="{realm}", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authentication required")

    def _ensure_base_path(self) -> bool:
        base_path = getattr(self.server, "base_path", "/")  # type: ignore[attr-defined]
        if base_path in ("", "/"):
            return True
        parsed = urlsplit(self.path)
        if not parsed.path.startswith(base_path):
            self.send_error(HTTPStatus.NOT_FOUND, "Base path mismatch")
            return False
        stripped_path = parsed.path[len(base_path) :] or "/"
        if not stripped_path.startswith("/"):
            stripped_path = f"/{stripped_path}"
        rebuilt = parsed._replace(path=stripped_path)
        self.path = urlunsplit(rebuilt)
        return True


###############################################################################
# Entrypoint
###############################################################################


def run_server(
    db_path: str,
    base_path: str = "/",
    prefer_ipv6: bool = False,
    unix_socket: Optional[str] = None,
    settings_path: Optional[str] = None,
) -> None:
    db_path = strip_wrapping_quotes(db_path) or DEFAULT_DB_PATH
    base_path = strip_wrapping_quotes(base_path) or "/"
    settings_path = strip_wrapping_quotes(settings_path) or strip_wrapping_quotes(
        os.environ.get("SCHEDULER_SETTINGS_PATH", DEFAULT_SETTINGS_PATH)
    ) or DEFAULT_SETTINGS_PATH

    settings = SchedulerSettings(settings_path)
    database = Database(
        db_path, result_retention_per_task=settings.result_retention_per_task
    )
    engine = SchedulerEngine(database, settings)
    ctx = SchedulerContext(database, engine, settings)
    handler_class = SchedulerRequestHandler
    normalized_base = normalize_base_path(base_path)

    # Note: authentication and TLS options are intentionally ignored
    # because front-end (index.cgi) handles authentication and TLS termination.
    # Use internal defaults for TCP bind if needed; CLI no longer exposes host/port.
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    if unix_socket:
        httpd = SchedulerHTTPServer(
            (host, port),
            handler_class,
            base_path=normalized_base,
            prefer_ipv6=prefer_ipv6,
            unix_socket_path=unix_socket,
            bind_and_activate=False,
        )
    else:
        httpd = SchedulerHTTPServer(
            (host, port),
            handler_class,
            base_path=normalized_base,
            prefer_ipv6=prefer_ipv6,
        )
    httpd.app_context = ctx  # type: ignore[attr-defined]

    # Setup TLS if needed (not typical for backend service)
    scheme = "http"

    shutdown_event = threading.Event()
    created_unix_socket = unix_socket if unix_socket else None

    def _handle_signal(signum: int, _: Any | None) -> None:
        if shutdown_event.is_set():
            return
        shutdown_event.set()
        logger.info("Received signal %s, shutting down scheduler...", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            signal.signal(getattr(signal, sig_name), _handle_signal)

    if unix_socket:
        logger.info(
            "Starting scheduler on %s+unix://%s%s (db=%s)",
            scheme,
            created_unix_socket,
            normalized_base,
            db_path,
        )
    else:
        logger.info(
            "Starting scheduler on %s://%s:%s%s (db=%s)",
            scheme,
            host,
            port,
            normalized_base,
            db_path,
        )
    engine.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down scheduler...")
    finally:
        engine.stop()
        database.close()
        httpd.server_close()
        # cleanup unix socket file if we created one
        if created_unix_socket:
            try:
                if os.path.exists(created_unix_socket):
                    os.unlink(created_unix_socket)
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduler Service")
    parser.add_argument(
        "--unix-socket",
        dest="unix_socket",
        default=os.environ.get("SCHEDULER_UNIX_SOCKET", DEFAULT_SOCKET_PATH),
        help="Path to UNIX domain socket to bind (default: system temp fn-scheduler.sock)",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("SCHEDULER_DB_PATH", DEFAULT_DB_PATH),
        help="Path to SQLite database file",
    )
    parser.add_argument(
        "--settings",
        default=os.environ.get("SCHEDULER_SETTINGS_PATH", DEFAULT_SETTINGS_PATH),
        help="Path to scheduler settings JSON file",
    )
    parser.add_argument(
        "--base-path",
        default=os.environ.get("SCHEDULER_BASE_PATH", "/"),
        help="Base URL path to mount the scheduler under (default '/')",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_server(
        args.db,
        base_path=args.base_path,
        prefer_ipv6=False,
        unix_socket=args.unix_socket,
        settings_path=args.settings,
    )
