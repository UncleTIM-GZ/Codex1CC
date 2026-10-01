"""SQLite state. Every mutation runs on the daemon event loop."""

from __future__ import annotations

import json
import os
import signal
import secrets
import shutil
import sqlite3
import time
import uuid
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
        self.on_event = None
        private_dir(path.parent)
        existing_database = path.exists()
        if path.is_symlink():
            raise BridgeError("INVALID_CONFIG", "State database must not be a symbolic link")
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if existing_database and self.db.execute("PRAGMA user_version").fetchone()[0] < 4:
            backup_path = path.with_name(path.name + f".pre-v4-{uuid.uuid4().hex[:8]}.bak")
            fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            try:
                with sqlite3.connect(backup_path) as backup:
                    self.db.backup(backup)
            except Exception:
                backup_path.unlink(missing_ok=True)
                raise
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL, prompt TEXT NOT NULL, acceptance TEXT NOT NULL,
            session_id TEXT, round_no INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            exit_code INTEGER, exit_reason TEXT, usage_json TEXT,
            snapshot_path TEXT, review_note TEXT, process_id INTEGER,
            bundle_json TEXT, project_json TEXT, result_json TEXT,
            complete_request_id TEXT UNIQUE, cancel_request_id TEXT UNIQUE
          );
          CREATE TABLE IF NOT EXISTS rounds (
            task_id TEXT NOT NULL, round_no INTEGER NOT NULL, instruction TEXT NOT NULL,
            status TEXT NOT NULL, started_at REAL, finished_at REAL,
            session_id TEXT, usage_json TEXT, exit_code INTEGER, request_id TEXT,
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
          DROP INDEX IF EXISTS active_project_one;
          CREATE INDEX IF NOT EXISTS active_project_lookup ON tasks(project_id,status);
          CREATE TABLE IF NOT EXISTS handoff_events (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, round_no INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, binding_json TEXT NOT NULL,
            receipt_token TEXT NOT NULL,
            summary_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
            turn_id TEXT, error TEXT, outcome TEXT, codex_status TEXT, codex_result TEXT,
            UNIQUE(task_id,event_key), FOREIGN KEY(task_id) REFERENCES tasks(id)
          );
          CREATE INDEX IF NOT EXISTS handoff_ready ON handoff_events(status,next_attempt_at);
          CREATE TABLE IF NOT EXISTS goals (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id), status TEXT NOT NULL,
            objective TEXT NOT NULL, acceptance_json TEXT NOT NULL,
            authorized_paths_json TEXT NOT NULL, reason TEXT,
            recovery_no INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS notifications (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
            title TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL,
            delivered_at REAL, error TEXT, attempted INTEGER NOT NULL DEFAULT 0
          );
        """)
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        for name in ("bundle_json", "project_json", "result_json", "process_id",
                     "complete_request_id", "cancel_request_id"):
            if name not in existing:
                kind = "INTEGER" if name == "process_id" else "TEXT"
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
        round_columns = {row[1] for row in self.db.execute("PRAGMA table_info(rounds)")}
        if "request_id" not in round_columns:
            self.db.execute("ALTER TABLE rounds ADD COLUMN request_id TEXT")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS round_request_one ON rounds(request_id) WHERE request_id IS NOT NULL")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS complete_request_one ON tasks(complete_request_id) WHERE complete_request_id IS NOT NULL")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS cancel_request_one ON tasks(cancel_request_id) WHERE cancel_request_id IS NOT NULL")
        self.db.execute("PRAGMA user_version=4")
        self.db.commit()

    def one(self, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise BridgeError("TASK_NOT_FOUND", "Task does not exist")
        item = dict(row)
        item["usage"] = json.loads(item.pop("usage_json") or "null")
        for column, key in (("bundle_json", "bundle"), ("project_json", "project"), ("result_json", "result")):
            item[key] = json.loads(item.pop(column) or "null")
        item["goal"] = self.goal(task_id)
        return item

    def goal(self, task_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM goals WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["acceptance"] = json.loads(result.pop("acceptance_json"))
        result["authorized_paths"] = json.loads(result.pop("authorized_paths_json"))
        return result

    def create_goal(self, task_id: str, bundle: dict, project: dict) -> None:
        now = time.time()
        self.db.execute("""INSERT INTO goals
            (task_id,status,objective,acceptance_json,authorized_paths_json,created_at,updated_at)
            VALUES(?,'active',?,?,?,?,?)""", (task_id, bundle["objective"],
            json.dumps(bundle["acceptance"], ensure_ascii=False),
            json.dumps(bundle.get("authorized_scope") or project["write_backend"]["write_paths"]), now, now))

    def set_goal(self, task_id: str, status: str, reason: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE goals SET status=?,reason=?,updated_at=? WHERE task_id=?",
                            (status, scrub(reason), time.time(), task_id))
        self.event(task_id, "goal_status", {"status": status, "reason": reason})
        self.notify(task_id, "Codex1CC · " + status, reason or "Goal " + status)

    def notify(self, task_id: str, title: str, message: str) -> None:
        task = self.db.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task:
            message = f"[{task['project_id']} · {task_id[:8]}] " + message
        with self.db:
            self.db.execute("INSERT INTO notifications(task_id,title,message,created_at) VALUES(?,?,?,?)",
                            (task_id, scrub(title[:100]), scrub(message[:2000]), time.time()))
        if self.on_event:
            self.on_event()

    def recover_goal(self, task_id: str) -> None:
        """Create a new decision event, never replay a previous Codex turn."""
        item = self.one(task_id)
        with self.db:
            self.db.execute("UPDATE goals SET recovery_no=recovery_no+1,updated_at=? WHERE task_id=?",
                            (time.time(), task_id))
            questions = self.pending_questions(task_id)
            self._enqueue_handoff(task_id, "goal_recovery", {
                "recovery_no": self.goal(task_id)["recovery_no"],
                "question_id": questions[0]["id"] if questions else "",
                "reason": "Goal incomplete with no active worker or controller"})
        self.event(task_id, "goal_recovery", {"status": item["status"]})

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
            if bundle.get("autonomous"):
                self.create_goal(task_id, bundle, project)

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
            item["goal"] = self.goal(item["id"])
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
        with self.db:
            cur = self.db.execute("INSERT INTO events(task_id,kind,data_json,created_at) VALUES(?,?,?,?)",
                                  (task_id, kind, payload, time.time()))
            self._enqueue_handoff(task_id, kind, data)
            seq = cur.lastrowid
        if self.on_event:
            self.on_event()
        return seq

    def transition(self, task_id: str, kind: str, data: dict,
                   question: tuple | None = None,
                   before: tuple[str, tuple] | None = None, **fields) -> int:
        """Write a task state change and its handoff event in one transaction."""
        fields["updated_at"] = time.time()
        payload = json.dumps(scrub(data), ensure_ascii=False, separators=(",", ":"))
        if len(payload) > 32768:
            payload = json.dumps({"truncated": True, "preview": payload[:16000]}, ensure_ascii=False)
        with self.db:
            if before is not None:
                self.db.execute(before[0], before[1])
            if question is not None:
                self.db.execute("""INSERT INTO questions
                    (id,task_id,round_no,text,options_json,status,created_at)
                    VALUES(?,?,?,?,?,'pending',?)""", question)
            keys = list(fields)
            self.db.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
                            [fields[k] for k in keys] + [task_id])
            goal_status = {"completed": "completed", "canceled": "canceled",
                           "continuing": "active"}.get(fields.get("status"))
            if goal_status:
                self.db.execute("UPDATE goals SET status=?,reason=NULL,updated_at=? WHERE task_id=?",
                                (goal_status, time.time(), task_id))
            cur = self.db.execute("INSERT INTO events(task_id,kind,data_json,created_at) VALUES(?,?,?,?)",
                                  (task_id, kind, payload, time.time()))
            self._enqueue_handoff(task_id, kind, data)
            seq = cur.lastrowid
        if self.on_event:
            self.on_event()
        if goal_status and self.goal(task_id):
            self.notify(task_id, "Codex1CC · " + goal_status,
                        data.get("review_note") or data.get("instruction") or "Goal " + goal_status)
        return seq

    def _enqueue_handoff(self, task_id: str, kind: str, data: dict) -> None:
        if kind not in {"question", "review_required", "failed", "interrupted", "question_expired", "goal_recovery"}:
            return
        row = self.db.execute("SELECT round_no,bundle_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or not row["bundle_json"]:
            return
        binding = json.loads(row["bundle_json"]).get("handoff")
        if not isinstance(binding, dict) or binding.get("mode") != "automatic":
            return
        now = time.time()
        event_key = f"{row['round_no']}:{kind}:{data.get('question_id', '')}"
        if kind == "goal_recovery":
            event_key += f":{data.get('recovery_no', self.goal(task_id)['recovery_no'])}"
        self.db.execute("""INSERT OR IGNORE INTO handoff_events
            (id,task_id,round_no,event_key,kind,status,binding_json,receipt_token,summary_json,created_at,updated_at,next_attempt_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, task_id, row["round_no"], event_key, kind, "pending",
             json.dumps(binding), secrets.token_urlsafe(32),
             json.dumps(scrub(data), ensure_ascii=False), now, now, now))

    def handoff_ready(self, limit: int = 10) -> list[dict]:
        busy = self.handoff_busy_threads()
        rows = self.db.execute("""SELECT * FROM handoff_events WHERE status='pending'
            AND next_attempt_at<=? ORDER BY created_at""", (time.time(),))
        ready = []
        for row in rows:
            item = self._handoff_row(row)
            if item["binding"].get("thread_id", "") not in busy:
                ready.append(item)
                if len(ready) >= limit:
                    break
        return ready

    def handoff_next_due(self) -> float | None:
        row = self.db.execute("SELECT MIN(next_attempt_at) FROM handoff_events WHERE status='pending' AND next_attempt_at>?",
                              (time.time(),)).fetchone()
        return row[0] if row else None

    def handoff_accepted(self) -> list[dict]:
        return [self._handoff_row(row) for row in self.db.execute(
            "SELECT * FROM handoff_events WHERE status='accepted' ORDER BY created_at")]

    def handoff_sending(self) -> list[dict]:
        return [self._handoff_row(row) for row in self.db.execute(
            "SELECT * FROM handoff_events WHERE status='sending' ORDER BY created_at")]

    def handoff_unrecorded(self) -> list[dict]:
        return [self._handoff_row(row) for row in self.db.execute(
            "SELECT * FROM handoff_events WHERE status='handled' AND turn_id IS NOT NULL "
            "AND codex_status IS NULL ORDER BY created_at")]

    def handoff_busy_threads(self) -> set[str]:
        rows = self.db.execute("""SELECT * FROM handoff_events
            WHERE status IN ('accepted','sending','needs_reconcile')""").fetchall()
        return {self._handoff_row(row)["binding"].get("thread_id", "") for row in rows}

    def handoff_started_count(self, task_id: str) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM handoff_events WHERE task_id=? AND turn_id IS NOT NULL",
                              (task_id,)).fetchone()
        return row[0]

    def handoff_one(self, event_id: str) -> dict:
        row = self.db.execute("SELECT * FROM handoff_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise BridgeError("TASK_NOT_FOUND", "Handoff event does not exist")
        return self._handoff_row(row)

    @staticmethod
    def _handoff_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["binding"] = json.loads(item.pop("binding_json"))
        item["summary"] = json.loads(item.pop("summary_json"))
        return item

    def handoff_status(self, task_id: str) -> dict:
        rows = self.db.execute("SELECT status,COUNT(*) AS count FROM handoff_events WHERE task_id=? GROUP BY status",
                               (task_id,)).fetchall()
        latest = self.db.execute("""SELECT id,status,kind,turn_id,error,outcome,codex_status,codex_result,updated_at
            FROM handoff_events WHERE task_id=? ORDER BY created_at DESC LIMIT 1""", (task_id,)).fetchone()
        attention = self.db.execute("""SELECT id,status,kind,turn_id,error,updated_at
            FROM handoff_events WHERE task_id=? AND status IN ('pending','sending','needs_reconcile','disabled')
            ORDER BY created_at LIMIT 10""", (task_id,)).fetchall()
        return {"counts": {r["status"]: r["count"] for r in rows},
                "latest": dict(latest) if latest else None,
                "attention": [dict(row) for row in attention]}

    def handoff_accept(self, event_id: str, turn_id: str) -> None:
        with self.db:
            self.db.execute("""UPDATE handoff_events SET status='accepted',turn_id=?,updated_at=?,error=NULL
                WHERE id=? AND status IN ('pending','sending')""", (turn_id, time.time(), event_id))

    def handoff_begin(self, event_id: str) -> bool:
        with self.db:
            changed = self.db.execute("""UPDATE handoff_events SET status='sending',updated_at=?
                WHERE id=? AND status='pending'""", (time.time(), event_id))
        return bool(changed.rowcount)

    def handoff_retry(self, event_id: str, reason: str, *, uncertain: bool = False) -> None:
        row = self.handoff_one(event_id)
        attempts = row["attempts"] + 1
        status = "needs_reconcile" if uncertain or attempts >= 3 else "pending"
        delay = min(60, 2 ** attempts)
        with self.db:
            self.db.execute("""UPDATE handoff_events SET status=?,attempts=?,next_attempt_at=?,
                updated_at=?,error=? WHERE id=? AND status IN ('pending','sending')""",
                (status, attempts, time.time() + delay, time.time(), str(scrub(reason))[:500], event_id))

    def handoff_defer(self, event_id: str, reason: str, delay: float = 60) -> None:
        previous = self.handoff_one(event_id)
        with self.db:
            self.db.execute("""UPDATE handoff_events SET status='pending',next_attempt_at=?,updated_at=?,error=?
                WHERE id=? AND status IN ('pending','sending')""",
                (time.time() + delay, time.time(), str(scrub(reason))[:500], event_id))
        if previous.get("error") != str(scrub(reason))[:500] and reason != "Codex session is busy":
            self.notify(previous["task_id"], "Codex1CC · connection delayed", reason)

    def handoff_awaken(self, event_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE handoff_events SET next_attempt_at=? WHERE id=? AND status='pending'",
                            (time.time(), event_id))

    def handoff_handle(self, event_id: str, turn_id: str, outcome: str) -> dict:
        item = self.handoff_one(event_id)
        if item["status"] == "handled":
            if item["turn_id"] == turn_id and item["outcome"] == outcome:
                return item
            raise BridgeError("INVALID_STATE", "Handoff event already handled differently")
        if item["status"] not in {"accepted", "needs_reconcile"} or item["turn_id"] != turn_id:
            raise BridgeError("INVALID_STATE", "Handoff was not accepted for this turn")
        with self.db:
            self.db.execute("UPDATE handoff_events SET status='handled',outcome=?,updated_at=? WHERE id=?",
                            (outcome, time.time(), event_id))
        return self.handoff_one(event_id)

    def handoff_flag(self, event_id: str, reason: str) -> None:
        with self.db:
            changed = self.db.execute("""UPDATE handoff_events SET status='needs_reconcile',error=?,updated_at=?
                WHERE id=? AND status IN ('pending','sending','accepted')""",
                (str(scrub(reason))[:500], time.time(), event_id))
        if changed.rowcount:
            task_id = self.handoff_one(event_id)["task_id"]
            self.event(task_id, "handoff_attention", {"event_id": event_id, "reason": reason})

    def handoff_record_turn(self, event_id: str, status: str, result: str | None) -> None:
        previous = self.handoff_one(event_id)
        with self.db:
            self.db.execute("""UPDATE handoff_events SET codex_status=?,codex_result=?,updated_at=?
                WHERE id=? AND turn_id IS NOT NULL""",
                (status[:40], scrub(result[:2000]) if isinstance(result, str) else None,
                 time.time(), event_id))
        if previous["codex_status"] != status and status in {"completed", "failed", "interrupted", "unknown"}:
            self.notify(previous["task_id"], "Codex1CC · Codex " + status,
                        result or "Codex review ended: " + status)

    def handoff_supersede(self, event_id: str) -> None:
        with self.db:
            self.db.execute("""UPDATE handoff_events SET status='handled',outcome='superseded',updated_at=?
                WHERE id=? AND status='pending'""", (time.time(), event_id))

    def handoff_suspend_project(self, project_id: str) -> int:
        with self.db:
            result = self.db.execute("""UPDATE handoff_events SET status='disabled',error='Automatic handoff disabled',
                updated_at=? WHERE task_id IN (SELECT id FROM tasks WHERE project_id=?) AND status='pending'""",
                (time.time(), project_id))
        return result.rowcount

    def handoff_stop(self, event_id: str, reason: str) -> None:
        with self.db:
            self.db.execute("""UPDATE handoff_events SET status='disabled',error=?,updated_at=?
                WHERE id=? AND status='pending'""", (str(scrub(reason))[:500], time.time(), event_id))

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
        task = self.db.execute("SELECT id,status,bundle_json FROM tasks WHERE request_id=?",
                               (request_id,)).fetchone()
        if task:
            if method != "submit_task":
                raise BridgeError("INVALID_ARGUMENT", "request_id already used for another operation")
            bundle = json.loads(task["bundle_json"] or "{}")
            mode = bundle.get("handoff", {}).get("mode", "manual")
            return {"task_id": task["id"], "status": task["status"],
                    "backend": bundle.get("backend", "read_only"),
                    "workspace": bundle.get("workspace"),
                    "handoff": {"mode": mode, "status": "connected" if mode == "automatic" else "disabled"}}
        question = self.db.execute("SELECT id,task_id FROM questions WHERE request_id=?",
                                   (request_id,)).fetchone()
        if question:
            if method != "respond_task":
                raise BridgeError("INVALID_ARGUMENT", "request_id already used for another operation")
            return {"task_id": question["task_id"], "question_id": question["id"], "status": "accepted"}
        round_row = self.db.execute("SELECT task_id,round_no FROM rounds WHERE request_id=?",
                                    (request_id,)).fetchone()
        if round_row:
            if method != "continue_task":
                raise BridgeError("INVALID_ARGUMENT", "request_id already used for another operation")
            return {"task_id": round_row["task_id"], "round_no": round_row["round_no"],
                    "status": "continuing"}
        for column, expected, status in (("complete_request_id", "complete_task", "completed"),
                                          ("cancel_request_id", "cancel_task", "canceled")):
            row = self.db.execute(f"SELECT id FROM tasks WHERE {column}=?", (request_id,)).fetchone()
            if row:
                if method != expected:
                    raise BridgeError("INVALID_ARGUMENT", "request_id already used for another operation")
                return {"task_id": row["id"], "status": status}
        return None

    def save_operation(self, request_id: str, method: str, response: dict) -> None:
        self.db.execute("INSERT INTO operations VALUES(?,?,?)",
                        (request_id, method, json.dumps(response, ensure_ascii=False)))
        self.db.commit()

    def mark_interrupted(self) -> None:
        rows = self.db.execute("SELECT id,process_id,bundle_json FROM tasks WHERE status IN ('running','waiting_answer','continuing')").fetchall()
        for row in rows:
            pid = row["process_id"]
            bundle = json.loads(row["bundle_json"] or "{}")
            workspace = bundle.get("workspace") or {}
            reason = "daemon_restart"
            if pid and os.name == "posix" and Path(f"/proc/{pid}/cmdline").exists():
                try:
                    command = Path(f"/proc/{pid}/cmdline").read_bytes()
                    cwd = Path(f"/proc/{pid}/cwd").resolve()
                    if (row["id"].encode() in command or
                            (workspace and cwd == Path(workspace["worktree_path"]).resolve()
                             and b"claude" in command)):
                        os.killpg(pid, signal.SIGTERM)
                    else:
                        reason = "daemon_restart_unverified_process"
                except (OSError, ProcessLookupError):
                    reason = "daemon_restart_unverified_process"
            self.transition(row["id"], "interrupted", {"reason": reason, "review_required": True},
                            status="interrupted", exit_reason=reason, process_id=None)
        self.db.execute("UPDATE questions SET status='expired' WHERE status='pending'")
        self.db.commit()

    def queued(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id,project_id FROM tasks WHERE status='queued' ORDER BY created_at")]

    def busy(self, project_id: str, exclude: str | None = None) -> bool:
        row = self.db.execute("SELECT id FROM tasks WHERE project_id=? AND status IN ('queued','running','waiting_answer','continuing') AND id != ? LIMIT 1",
                              (project_id, exclude or "")).fetchone()
        return row is not None

    def active(self, project_id: str, exclude: str | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,status,bundle_json FROM tasks WHERE project_id=? "
            "AND status IN ('queued','running','waiting_answer','continuing') AND id!=? "
            "ORDER BY created_at",
            (project_id, exclude or "")).fetchall()
        return [{"id": row["id"], "status": row["status"],
                 "bundle": json.loads(row["bundle_json"] or "{}")}
                for row in rows]

    def prune_terminal_history(self, retention_days: int = 30) -> int:
        """Remove old task content; keep minimal IDs and terminal statuses."""
        cutoff = time.time() - retention_days * 86400
        rows = self.db.execute(
            "SELECT id,snapshot_path,bundle_json FROM tasks WHERE status IN ('completed','failed','canceled') "
            "AND updated_at<? AND bundle_json IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM handoff_events h WHERE h.task_id=tasks.id "
            "AND h.status!='handled')", (cutoff,)).fetchall()
        snapshots = (STATE / "snapshots").resolve()
        pruned = 0
        for row in rows:
            if json.loads(row["bundle_json"] or "{}").get("backend") == "native_write":
                continue  # Worktrees and branch provenance require explicit user cleanup.
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
            pruned += 1
        return pruned
