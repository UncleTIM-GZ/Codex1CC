"""Existing task histories survive the event-driven schema upgrade."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex1cc.store import Store


class StoreMigrationTest(unittest.TestCase):
    def test_old_database_is_backed_up_and_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE tasks (
                      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                      status TEXT NOT NULL, prompt TEXT NOT NULL, acceptance TEXT NOT NULL,
                      session_id TEXT, round_no INTEGER NOT NULL DEFAULT 0,
                      created_at REAL NOT NULL, updated_at REAL NOT NULL,
                      exit_code INTEGER, exit_reason TEXT, usage_json TEXT,
                      snapshot_path TEXT, review_note TEXT, process_id INTEGER,
                      bundle_json TEXT, project_json TEXT, result_json TEXT
                    );
                    CREATE TABLE rounds (
                      task_id TEXT NOT NULL, round_no INTEGER NOT NULL, instruction TEXT NOT NULL,
                      status TEXT NOT NULL, started_at REAL, finished_at REAL,
                      session_id TEXT, usage_json TEXT, exit_code INTEGER,
                      PRIMARY KEY(task_id,round_no)
                    );
                    INSERT INTO tasks(id,project_id,request_id,status,prompt,acceptance,
                                      created_at,updated_at,bundle_json)
                    VALUES('oldtask','demo','oldrequest','review_required','Read','[]',1,1,'{}');
                    INSERT INTO rounds(task_id,round_no,instruction,status)
                    VALUES('oldtask',1,'Read','completed');
                """)
            store = Store(path)
            try:
                self.assertEqual(store.one("oldtask")["status"], "review_required")
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertIn("request_id", {row[1] for row in store.db.execute("PRAGMA table_info(rounds)")})
                self.assertEqual(len(list(Path(directory).glob("state.sqlite3.pre-v3-*.bak"))), 1)
            finally:
                store.db.close()
