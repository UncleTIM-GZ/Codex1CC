"""Exercise a real daemon and its durable handoff outbox without model calls."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from tests.test_executor import request


class HandoffFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        root = base / "project"
        root.mkdir()
        (root / "README.txt").write_text("public content", encoding="utf-8")
        (root / "CONTEXT.txt").write_text("shared fact", encoding="utf-8")
        cli = base / "fake-claude"
        cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'type':'result','session_id':'fake-session',"
            "'result':'Public content checked','is_error':False,'total_cost_usd':0.001}),flush=True)\n",
            encoding="utf-8")
        cli.chmod(0o755)
        self.state = base / "state"
        self.cli = cli
        config = base / "projects.json"
        config.write_text(json.dumps({"projects": {"sample": {
            "root": str(root), "shared_context": "CONTEXT.txt", "read_paths": ["README.txt"],
            "claude_path": str(cli), "handoff": {"mode": "automatic", "thread_id": "test-thread"},
            "limits": {"seconds": 30}}}}), encoding="utf-8")
        config.chmod(0o600)
        self.env = os.environ.copy()
        self.env.update({"CODEX1CC_STATE_DIR": str(self.state), "CODEX1CC_CONFIG": str(config),
                         "PYTHONPATH": os.pathsep.join((str(Path(__file__).resolve().parents[1] / "src"),
                                                         str(Path(__file__).resolve().parents[1])))})
        self.start_daemon()

    def start_daemon(self) -> None:
        self.daemon = subprocess.Popen([sys.executable, "-m", "tests.fake_handoff_daemon"],
                                       env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.socket = self.state / "daemon.sock"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.socket.exists():
                return
            time.sleep(0.02)
        self.fail(self.daemon.stderr.read().decode())

    def tearDown(self) -> None:
        self.daemon.terminate()
        try:
            self.daemon.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.daemon.kill()
            self.daemon.communicate()
        self.temp.cleanup()

    def call(self, method: str, params: dict) -> dict:
        return asyncio.run(request(self.socket, method, params))

    def receipt(self, event_id: str) -> str:
        with sqlite3.connect(self.state / "state.sqlite3") as connection:
            return connection.execute("SELECT receipt_token FROM handoff_events WHERE id=?",
                                      (event_id,)).fetchone()[0]

    def test_result_delivered_once_and_acknowledged(self) -> None:
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Read README", "context": "",
            "acceptance": ["README checked"], "deliverables": ["conclusion"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex,
            "handoff": "automatic"})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        self.assertEqual(started["data"]["handoff"]["status"], "connected")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            report = self.call("get_task", {"task_id": task_id})["data"]["task"]
            latest = report["handoff"]["latest"]
            if report["status"] == "review_required" and latest and latest["status"] == "accepted":
                break
            time.sleep(0.02)
        self.assertEqual(report["status"], "review_required", report)
        self.assertEqual(latest["status"], "accepted", report)
        event_id = latest["id"]
        delivered = (self.state / "delivered.jsonl").read_text().splitlines()
        self.assertEqual(len(delivered), 1)
        self.assertIn(event_id, json.loads(delivered[0])["prompt"])
        wrong = self.call("ack_handoff", {"event_id": event_id,
                                          "receipt_token": "wrong", "outcome": "reviewed"})
        self.assertFalse(wrong["ok"])
        first = self.call("ack_handoff", {"event_id": event_id,
                                          "receipt_token": self.receipt(event_id), "outcome": "reviewed"})
        again = self.call("ack_handoff", {"event_id": event_id,
                                          "receipt_token": self.receipt(event_id), "outcome": "reviewed"})
        self.assertEqual(first, again)
        self.assertEqual(first["data"]["status"], "handled")
        while time.monotonic() < deadline:
            latest = self.call("get_task", {"task_id": task_id})["data"]["task"]["handoff"]["latest"]
            if latest["codex_status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(latest["codex_result"], "Reviewed task")
        db = sqlite3.connect(self.state / "state.sqlite3")
        try:
            count = db.execute("SELECT COUNT(*) FROM handoff_events WHERE task_id=?", (task_id,)).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            db.close()

    def test_automatic_requires_registered_binding(self) -> None:
        config_path = Path(self.env["CODEX1CC_CONFIG"])
        config = json.loads(config_path.read_text())
        del config["projects"]["sample"]["handoff"]
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        result = self.call("submit_task", {
            "project_id": "sample", "objective": "Read README", "context": "",
            "acceptance": ["README checked"], "deliverables": ["conclusion"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex,
            "handoff": "automatic"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "HANDOFF_UNAVAILABLE")

    def test_question_then_result_are_separate_handoffs(self) -> None:
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, socket\n"
            "sock=socket.socket(socket.AF_UNIX)\n"
            "sock.connect(os.environ['CODEX1CC_STATE_DIR']+'/daemon.sock')\n"
            "req={'method':'ask_question','params':{'task_id':os.environ['CODEX1CC_TASK_ID'],"
            "'text':'Which option?','options':['A','B']}}\n"
            "sock.sendall((json.dumps(req)+'\\n').encode())\n"
            "answer=json.loads(sock.makefile('r').readline())['data']['answer']\n"
            "print(json.dumps({'type':'result','session_id':'fake-session',"
            "'result':'Selected '+answer,'is_error':False,'total_cost_usd':0.001}),flush=True)\n",
            encoding="utf-8")
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Ask then report", "context": "",
            "acceptance": ["Selection recorded"], "deliverables": ["Selection"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex,
            "handoff": "automatic"})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            report = self.call("get_task", {"task_id": task_id})["data"]
            latest = report["task"]["handoff"]["latest"]
            if report["task"]["status"] == "waiting_answer" and latest and latest["status"] == "accepted":
                break
            time.sleep(0.02)
        self.assertEqual(report["task"]["status"], "waiting_answer", report)
        question_event_id = latest["id"]
        question_id = report["questions"][0]["id"]
        answered = self.call("respond_task", {"task_id": task_id, "question_id": question_id,
                                             "answer": "B", "request_id": uuid.uuid4().hex})
        self.assertTrue(answered["ok"], answered)
        ack = self.call("ack_handoff", {"event_id": question_event_id,
                                        "receipt_token": self.receipt(question_event_id), "outcome": "answered"})
        self.assertEqual(ack["data"]["status"], "handled")
        while time.monotonic() < deadline:
            report = self.call("get_task", {"task_id": task_id})["data"]
            latest = report["task"]["handoff"]["latest"]
            if (report["task"]["status"] == "review_required" and latest and
                    latest["id"] != question_event_id and latest["status"] == "accepted"):
                break
            time.sleep(0.02)
        self.assertEqual(report["task"]["result"]["conclusion"], "Selected B")
        self.assertNotEqual(latest["id"], question_event_id)
        self.assertEqual(latest["status"], "accepted")
        delivered = (self.state / "delivered.jsonl").read_text().splitlines()
        self.assertEqual(len(delivered), 2)

    def test_restart_does_not_redeliver_accepted_turn(self) -> None:
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Read README", "context": "",
            "acceptance": ["README checked"], "deliverables": ["conclusion"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex,
            "handoff": "automatic"})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": task_id})["data"]["task"]
            latest = item["handoff"]["latest"]
            if latest and latest["status"] == "accepted":
                break
            time.sleep(0.02)
        self.assertEqual(latest["status"], "accepted")
        event_id = latest["id"]
        self.daemon.terminate()
        self.daemon.communicate(timeout=5)
        self.socket.unlink(missing_ok=True)
        self.start_daemon()
        after = self.call("get_task", {"task_id": task_id})["data"]["task"]
        self.assertEqual(after["handoff"]["latest"]["id"], event_id)
        self.assertEqual(after["handoff"]["latest"]["status"], "accepted")
        self.assertEqual(len((self.state / "delivered.jsonl").read_text().splitlines()), 1)
        ack = self.call("ack_handoff", {"event_id": event_id,
                                        "receipt_token": self.receipt(event_id), "outcome": "reviewed"})
        self.assertEqual(ack["data"]["status"], "handled")

    def test_uncertain_turn_can_be_explicitly_resolved_without_redelivery(self) -> None:
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Read README", "context": "",
            "acceptance": ["README checked"], "deliverables": ["conclusion"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex,
            "handoff": "automatic"})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            report = self.call("get_task", {"task_id": task_id})["data"]["task"]
            latest = report["handoff"]["latest"]
            if latest and latest["status"] == "accepted":
                break
            time.sleep(0.02)
        self.assertEqual(latest["status"], "accepted")
        event_id = latest["id"]
        with sqlite3.connect(self.state / "state.sqlite3") as connection:
            connection.execute("UPDATE handoff_events SET status='needs_reconcile' WHERE id=?", (event_id,))
        resolved = self.call("ack_handoff", {"event_id": event_id,
                                              "receipt_token": self.receipt(event_id),
                                              "outcome": "failed"})
        self.assertTrue(resolved["ok"], resolved)
        self.assertEqual(resolved["data"]["status"], "handled")
        self.assertEqual(len((self.state / "delivered.jsonl").read_text().splitlines()), 1)
