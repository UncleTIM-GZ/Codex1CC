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
                         "CODEX1CC_DESKTOP_NOTIFICATIONS": "0",
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

    def enable_write(self, *, automatic_controller: bool = False) -> Path:
        config_path = Path(self.env["CODEX1CC_CONFIG"])
        config = json.loads(config_path.read_text())
        root = Path(config["projects"]["sample"]["root"])
        for command in (["init", "-q"], ["config", "user.name", "Test User"],
                        ["config", "user.email", "test@example.invalid"], ["add", "."],
                        ["commit", "-qm", "baseline"]):
            subprocess.run(["git", "-C", str(root), *command], check=True)
        config["projects"]["sample"]["write_backend"] = {
            "enabled": True, "write_paths": ["README.txt", "FIX.txt"]}
        config_path.write_text(json.dumps(config))
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, socket, subprocess, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --strict-mcp-config --resume'); sys.exit(0)\n"
            "p=pathlib.Path('README.txt')\n"
            "if p.read_text() != 'partial':\n"
            " p.write_text('partial')\n"
            " subprocess.run(['git','add','README.txt'],check=True)\n"
            " subprocess.run(['git','commit','-qm','partial result'],check=True)\n"
            " sys.exit(3)\n"
            "assert '--resume' not in sys.argv\n"
            "sock=socket.socket(socket.AF_UNIX); sock.connect(os.environ['CODEX1CC_STATE_DIR']+'/daemon.sock')\n"
            "req={'method':'ask_question','params':{'task_id':os.environ['CODEX1CC_TASK_ID'],'text':'Verify repair?', 'options':['verified']}}\n"
            "sock.sendall((json.dumps(req)+'\\n').encode())\n"
            "answer=json.loads(sock.makefile('r').readline())['data']['answer']\n"
            "pathlib.Path('FIX.txt').write_text(answer); p.write_text('fixed')\n"
            "subprocess.run(['git','add','README.txt','FIX.txt'],check=True)\n"
            "subprocess.run(['git','commit','-qm','repair result'],check=True)\n"
            "print(json.dumps({'type':'result','session_id':'fresh','result':'fixed','is_error':False}),flush=True)\n")
        if automatic_controller:
            self.daemon.terminate()
            self.daemon.communicate(timeout=5)
            self.socket.unlink(missing_ok=True)
            self.env["CODEX1CC_TEST_AUTONOMOUS"] = "1"
            self.start_daemon()
        return root

    def submit_write(self, authorized_scope: list[str] | None = None) -> str:
        response = self.call("submit_task", {"project_id": "sample", "objective": "Repair and verify",
            "acceptance": ["README fixed and repair verified"], "deliverables": ["Verified commit"],
            "scope": ["README.txt"], "actions": ["read", "write", "execute"],
            "handoff": "automatic", "authorized_scope": authorized_scope, "request_id": uuid.uuid4().hex})
        self.assertTrue(response["ok"], response)
        return response["data"]["task_id"]

    def wait_status(self, task_id: str, goal_status: str | None = None) -> dict:
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            task = self.call("get_task", {"task_id": task_id})["data"]["task"]
            latest = task["handoff"]["latest"]
            if goal_status:
                if task.get("goal", {}).get("status") == goal_status:
                    return task
            elif latest and latest["status"] == "accepted":
                return task
            time.sleep(0.02)
        self.fail(str(task))

    def test_autonomous_failure_replan_question_and_acceptance_without_user_intervention(self) -> None:
        root = self.enable_write(automatic_controller=True)
        original_head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        task_id = self.submit_write()
        task = self.wait_status(task_id, "completed")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["round_no"], 2)
        self.assertEqual(task["request"]["scope"], ["FIX.txt", "README.txt"])
        self.assertEqual(task["goal"]["acceptance"], ["README fixed and repair verified"])
        self.assertEqual(len(task["result"]["workspace"]["commits"]), 2)
        self.assertEqual(task["result"]["workspace"]["outside_scope"], [])
        self.assertEqual(subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(), original_head)
        inbox = self.call("notifications", {})["data"]["notifications"]
        self.assertTrue(any(n["title"] == "Codex1CC · completed" for n in inbox))

    def test_active_goal_cannot_be_dropped_and_scope_replan_checks_authorization_and_head(self) -> None:
        self.enable_write()
        task_id = self.submit_write()
        task = self.wait_status(task_id)
        self.assertEqual(task["status"], "failed")
        latest = task["handoff"]["latest"]
        ack = self.call("ack_handoff", {"event_id": latest["id"],
            "receipt_token": self.receipt(latest["id"]), "outcome": "failed"})
        self.assertFalse(ack["ok"])
        continuation = {"task_id": task_id, "fresh_session": True, "scope": ["PRIVATE.txt"],
            "review_note": "Inspected partial commit", "expected_head": task["result"]["workspace"]["head_commit"],
            "instruction": "Repair", "request_id": uuid.uuid4().hex}
        rejected = self.call("continue_task", continuation)
        self.assertEqual(rejected["error"]["code"], "PROJECT_NOT_ALLOWED")
        continuation.update(scope=["README.txt", "FIX.txt"], expected_head="stale")
        rejected = self.call("continue_task", continuation)
        self.assertEqual(rejected["error"]["code"], "INVALID_STATE")
        config_path = Path(self.env["CODEX1CC_CONFIG"])
        config = json.loads(config_path.read_text())
        config["projects"]["sample"]["write_backend"]["write_paths"] = ["README.txt"]
        config_path.write_text(json.dumps(config))
        continuation["expected_head"] = task["result"]["workspace"]["head_commit"]
        revoked = self.call("continue_task", continuation)
        self.assertEqual(revoked["error"]["code"], "PROJECT_NOT_ALLOWED")
        blocked = self.call("manage_goal", {"task_id": task_id, "status": "blocked",
            "reason": "Provider authentication requires user action", "request_id": uuid.uuid4().hex})
        self.assertTrue(blocked["ok"], blocked)
        ack = self.call("ack_handoff", {"event_id": latest["id"],
            "receipt_token": self.receipt(latest["id"]), "outcome": "failed"})
        self.assertTrue(ack["ok"], ack)

    def test_whole_goal_path_restriction_survives_project_wide_repair_authorization(self) -> None:
        self.enable_write()
        task_id = self.submit_write(authorized_scope=["README.txt"])
        task = self.wait_status(task_id)
        rejected = self.call("continue_task", {"task_id": task_id, "fresh_session": True,
            "scope": ["README.txt", "FIX.txt"], "review_note": "Inspected original commit",
            "expected_head": task["result"]["workspace"]["head_commit"],
            "instruction": "Repair", "request_id": uuid.uuid4().hex})
        self.assertEqual(rejected["error"]["code"], "PROJECT_NOT_ALLOWED")
        self.assertEqual(task["goal"]["authorized_paths"], ["README.txt"])

    def test_autonomous_handoff_limit_records_blocker_instead_of_silent_stop(self) -> None:
        self.enable_write(automatic_controller=True)
        config_path = Path(self.env["CODEX1CC_CONFIG"])
        config = json.loads(config_path.read_text())
        config["projects"]["sample"]["handoff"]["max_turns"] = 1
        config_path.write_text(json.dumps(config))
        task = self.wait_status(self.submit_write(), "blocked")
        self.assertIn("turn limit", task["goal"]["reason"])
        self.assertEqual(task["status"], "waiting_answer")

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

    def test_native_write_result_reaches_bound_codex(self) -> None:
        config_path = Path(self.env["CODEX1CC_CONFIG"])
        config = json.loads(config_path.read_text())
        root = Path(config["projects"]["sample"]["root"])
        for command in (["init", "-q"], ["config", "user.name", "Test User"],
                        ["config", "user.email", "test@example.invalid"], ["add", "."],
                        ["commit", "-qm", "baseline"]):
            subprocess.run(["git", "-C", str(root), *command], check=True)
        config["projects"]["sample"]["write_backend"] = {
            "enabled": True, "write_paths": ["README.txt"]}
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, subprocess, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --max-budget-usd --strict-mcp-config --resume'); sys.exit(0)\n"
            "pathlib.Path('README.txt').write_text('edited')\n"
            "subprocess.run(['git','add','README.txt'],check=True)\n"
            "subprocess.run(['git','commit','-qm','edit'],check=True)\n"
            "print(json.dumps({'type':'result','session_id':'write-session',"
            "'result':'Edited','is_error':False,'total_cost_usd':0.001}),flush=True)\n",
            encoding="utf-8")
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Edit README", "context": "",
            "acceptance": ["README edited"], "deliverables": ["Local commit"],
            "scope": ["README.txt"], "actions": ["read", "write", "execute"],
            "request_id": uuid.uuid4().hex, "handoff": "automatic"})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            report = self.call("get_task", {"task_id": task_id})["data"]["task"]
            latest = report["handoff"]["latest"]
            if report["status"] == "review_required" and latest and latest["status"] == "accepted":
                break
            time.sleep(0.02)
        self.assertEqual(report["status"], "review_required", report)
        self.assertEqual(len(report["result"]["workspace"]["commits"]), 1)
        incomplete = self.call("complete_task", {"task_id": task_id,
            "review_note": "Would omit original acceptance evidence", "request_id": uuid.uuid4().hex})
        self.assertFalse(incomplete["ok"])
        completed = self.call("complete_task", {"task_id": task_id,
            "review_note": "Checked branch commit and README", "request_id": uuid.uuid4().hex,
            "acceptance_evidence": ["README content equals edited"],
            "expected_head": report["result"]["workspace"]["head_commit"]})
        self.assertEqual(completed["data"]["status"], "completed")
        ack = self.call("ack_handoff", {"event_id": latest["id"],
            "receipt_token": self.receipt(latest["id"]), "outcome": "completed"})
        self.assertEqual(ack["data"]["status"], "handled")
        self.assertEqual(len((self.state / "delivered.jsonl").read_text().splitlines()), 1)

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

    def test_configured_automatic_binding_is_inherited_when_handoff_is_omitted(self) -> None:
        self.enable_write()
        response = self.call("submit_task", {"project_id": "sample", "objective": "Repair",
            "acceptance": ["README fixed"], "deliverables": ["Commit"], "scope": ["README.txt"],
            "actions": ["read", "write", "execute"], "request_id": uuid.uuid4().hex})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"]["handoff"]["mode"], "automatic")
        self.assertEqual(response["data"]["goal"]["status"], "active")

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
