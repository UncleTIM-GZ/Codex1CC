"""SQLite state. Every mutation runs on the daemon event loop."""

from __future__ import annotations

import json
import os
import signal
import shutil
import sqlite3
import time
from pathlib import Path

from .common import STATE, BridgeError, private_dir

ACTIVE = ("queued", "running", "waiting_answer", "continuing")
REDACTED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
SECRET_VALUES: set[str] = set()


def register_secret(value: str) -> None:
    if len(value) > 8:
        SECRET_VALUES.add(value)


def scrub(value: object) -> object:
    """Bound event storage and omit credential-shaped object fields."""
    if isinstance(value, dict):
        def sensitive(key: str) -> bool:
            upper = key.upper()
            return (upper == "TOKEN" or upper.endswith("_TOKEN") or
                    any(word in upper for word in ("SECRET", "PASSWORD", "API_KEY", "AUTHORIZATION")))
        return {str(k)[:100]: ("[redacted]" if sensitive(str(k)) else scrub(v))
                for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [scrub(x) for x in value[:50]]
    if isinstance(value, str):
        result = value[:16000]
        for name in REDACTED:
            secret = os.environ.get(name)
            if secret and len(secret) > 8:
                result = result.replace(secret, "[redacted]")
        for secret in SECRET_VALUES:
            result = result.replace(secret, "[redacted]")
        return result
    return value


class Store:
    def __init__(self, path: Path = STATE / "state.sqlite3"):
        private_dir(path.parent)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL, prompt TEXT NOT NULL, acceptance TEXT NOT NULL,
            session_id TEXT, round_no INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            exit_code INTEGER, exit_reason TEXT, usage_json TEXT,
            snapshot_path TEXT, review_note TEXT, process_id INTEGER,
            bundle_json TEXT, project_json TEXT, result_json TEXT
          );
          CREATE TABLE IF NOT EXISTS rounds (
            task_id TEXT NOT NULL, round_no INTEGER NOT NULL, instruction TEXT NOT NULL,
            status TEXT NOT NULL, started_at REAL, finished_at REAL,
            session_id TEXT, usage_json TEXT, exit_code INTEGER,
            PRIMARY KEY(task_id, round_no), FOREIGN KEY(task_id) REFERENCES tasks(id)
          );
          CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, data_json TEXT NOT NULL, created_at REAL NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
          );
          CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, seq);
          CREATE TABLE IF NOT EXISTS questions (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, round_no INTEGER NOT NULL,
            text TEXT NOT NULL, options_json TEXT NOT NULL,
            status TEXT NOT NULL, answer TEXT, request_id TEXT UNIQUE,
            created_at REAL NOT NULL, answered_at REAL,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
          );
          CREATE TABLE IF NOT EXISTS operations (
            request_id TEXT PRIMARY KEY, method TEXT NOT NULL, response_json TEXT NOT NULL
          );
        """)
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        for name in ("bundle_json", "project_json", "result_json", "process_id"):
            if name not in existing:
                kind = "INTEGER" if name == "process_id" else "TEXT"
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
        self.db.commit()

    def one(self, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise BridgeError("TASK_NOT_FOUND", "Task does not exist")
        item = dict(row)
        item["usage"] = json.loads(item.pop("usage_json") or "null")
        for column, key in (("bundle_json", "bundle"), ("project_json", "project"), ("result_json", "result")):
            item[key] = json.loads(item.pop(column) or "null")
        return item

    def create(self, task_id: str, project_id: str, request_id: str, bundle: dict,
               project: dict, snapshot_path: str) -> None:
        now = time.time()
        with self.db:
            self.db.execute("""INSERT INTO tasks
                (id, project_id, request_id, status, prompt, acceptance, created_at,
                 updated_at, snapshot_path, bundle_json, project_json)
                VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, project_id, request_id, bundle["objective"],
                 json.dumps(bundle["acceptance"], ensure_ascii=False), now, now,
                 snapshot_path, json.dumps(bundle, ensure_ascii=False),
                 json.dumps(project, ensure_ascii=False)))
            self.db.execute("INSERT INTO rounds(task_id,round_no,instruction,status) VALUES(?,?,?,?)",
                            (task_id, 1, bundle["objective"], "queued"))

    def list_tasks(self, project_id: str | None, statuses: list[str] | None,
                   offset: int, limit: int) -> list[dict]:
        where = []
        values: list = []
        if project_id:
            where.append("project_id=?")
            values.append(project_id)
        if statuses:
            where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            values.extend(statuses)
        sql = "SELECT id,project_id,status,updated_at,result_json,exit_reason FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        values.extend((limit, offset))
        result = []
        for row in self.db.execute(sql, values):
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json") or "null")
            result.append(item)
        return result

    def pending_questions(self, task_id: str) -> list[dict]:
        result = []
        for row in self.db.execute(
            "SELECT id,text,options_json,created_at FROM questions WHERE task_id=? AND status='pending' ORDER BY created_at",
            (task_id,)):
            item = dict(row)
            item["options"] = json.loads(item.pop("options_json"))
            result.append(item)
        return result

    def event(self, task_id: str, kind: str, data: dict) -> int:
        payload = json.dumps(scrub(data), ensure_ascii=False, separators=(",", ":"))
        if len(payload) > 32768:
            payload = json.dumps({"truncated": True, "preview": payload[:16000]}, ensure_ascii=False)
        cur = self.db.execute("INSERT INTO events(task_id,kind,data_json,created_at) VALUES(?,?,?,?)",
                              (task_id, kind, payload, time.time()))
        self.db.commit()
        return cur.lastrowid

    def events(self, task_id: str, cursor: int, limit: int = 50) -> tuple[list, bool]:
        rows = self.db.execute("SELECT seq,kind,data_json,created_at FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                               (task_id, cursor, limit + 1)).fetchall()
        result = [{"seq": row["seq"], "kind": row["kind"], "data": json.loads(row["data_json"]),
                   "created_at": row["created_at"]} for row in rows[:limit]]
        return result, len(rows) > limit

    def update(self, task_id: str, **fields) -> None:
        fields["updated_at"] = time.time()
        keys = list(fields)
        self.db.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
                        [fields[k] for k in keys] + [task_id])
        self.db.commit()

    def operation(self, request_id: str, method: str) -> dict | None:
        row = self.db.execute("SELECT method,response_json FROM operations WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["method"] != method:
                raise BridgeError("INVALID_ARGUMENT", "request_id already used for another operation")
            return json.loads(row["response_json"])
        return None

    def save_operation(self, request_id: str, method: str, response: dict) -> None:
        self.db.execute("INSERT INTO operations VALUES(?,?,?)",
                        (request_id, method, json.dumps(response, ensure_ascii=False)))
        self.db.commit()

    def mark_interrupted(self) -> None:
        rows = self.db.execute("SELECT id,process_id FROM tasks WHERE status IN ('running','waiting_answer','continuing')").fetchall()
        for row in rows:
            pid = row["process_id"]
            if pid and os.name == "posix" and Path(f"/proc/{pid}/cmdline").exists():
                try:
                    command = Path(f"/proc/{pid}/cmdline").read_bytes()
                    if row["id"].encode() in command:
                        os.killpg(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
            self.update(row["id"], status="interrupted", exit_reason="daemon_restart", process_id=None)
            self.event(row["id"], "interrupted", {"reason": "daemon_restart", "review_required": True})
        self.db.execute("UPDATE questions SET status='expired' WHERE status='pending'")
        self.db.commit()

    def queued(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id,project_id FROM tasks WHERE status='queued' ORDER BY created_at")]

    def busy(self, project_id: str, exclude: str | None = None) -> bool:
        row = self.db.execute("SELECT id FROM tasks WHERE project_id=? AND status IN ('queued','running','waiting_answer','continuing') AND id != ? LIMIT 1",
                              (project_id, exclude or "")).fetchone()
        return row is not None

    def prune_terminal_history(self, retention_days: int = 30) -> int:
        """Remove old task content; keep minimal IDs and terminal statuses."""
        cutoff = time.time() - retention_days * 86400
        rows = self.db.execute(
            "SELECT id,snapshot_path FROM tasks WHERE status IN ('completed','failed','canceled') "
            "AND updated_at<? AND bundle_json IS NOT NULL", (cutoff,)).fetchall()
        snapshots = (STATE / "snapshots").resolve()
        for row in rows:
            path_text = row["snapshot_path"]
            if path_text:
                path = Path(path_text)
                if path.parent.resolve() == snapshots and not path.is_symlink() and path.is_dir():
                    for directory, children, _ in os.walk(path):
                        os.chmod(directory, 0o700)
                        children[:] = [name for name in children if not (Path(directory) / name).is_symlink()]
                    shutil.rmtree(path)
            (STATE / "mcp" / f"{row['id']}.json").unlink(missing_ok=True)
            with self.db:
                self.db.execute("DELETE FROM events WHERE task_id=?", (row["id"],))
                self.db.execute("DELETE FROM questions WHERE task_id=?", (row["id"],))
                self.db.execute("DELETE FROM rounds WHERE task_id=?", (row["id"],))
                self.db.execute(
                    "UPDATE tasks SET prompt='[expired]',acceptance='[expired]',bundle_json=NULL,"
                    "project_json=NULL,result_json=NULL,snapshot_path=NULL,review_note=NULL,"
                    "usage_json=NULL WHERE id=?", (row["id"],))
        return len(rows)
