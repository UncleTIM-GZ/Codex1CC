"""End-to-end daemon test with a fake CLI. No model request is made."""

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


async def request(socket: Path, method: str, params: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(socket))
    writer.write((json.dumps({"method": method, "params": params}) + "\n").encode())
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return response


class ExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "README.txt").write_text("public task content", encoding="utf-8")
        (self.root / "CONTEXT.txt").write_text("shared project fact", encoding="utf-8")
        (self.root / "PRIVATE.txt").write_text("private marker", encoding="utf-8")
        self.cli = base / "fake-claude"
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'type':'system','subtype':'init','session_id':'fake-session'}), flush=True)\n"
            "print(json.dumps({'type':'result','session_id':'fake-session','result':'Done: README checked',"
            "'is_error':False,'usage':{'input_tokens':1}}), flush=True)\n",
            encoding="utf-8",
        )
        self.cli.chmod(0o755)
        self.state = base / "state"
        self.config = base / "projects.json"
        self.config.write_text(json.dumps({"projects": {"sample": {
            "root": str(self.root), "shared_context": "CONTEXT.txt",
            "read_paths": ["README.txt"], "limits": {"seconds": 30},
            "claude_path": str(self.cli)}}}), encoding="utf-8")
        self.config.chmod(0o600)
        self.env = os.environ.copy()
        self.env.update({"CODEX1CC_STATE_DIR": str(self.state),
                         "CODEX1CC_CONFIG": str(self.config),
                         "PYTHONPATH": os.pathsep.join((
                             str(Path(__file__).resolve().parents[1] / "src"),
                             str(Path(__file__).resolve().parents[1])))})
        self.daemon = subprocess.Popen(
            [sys.executable, "-m", "tests.fake_daemon"], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.socket = self.state / "daemon.sock"
        deadline = time.monotonic() + 5
        while not self.socket.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.socket.exists():
            self.fail(f"daemon did not start: {self.daemon.stderr.read().decode()}")

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

    def test_complete_task_and_snapshot_boundary(self) -> None:
        bundle = {"project_id": "sample", "objective": "Read the public file",
                  "context": "Only report a conclusion", "acceptance": ["README inspected"],
                  "deliverables": ["Short result"], "scope": ["README.txt"],
                  "limits": {"seconds": 10}, "request_id": uuid.uuid4().hex}
        first = self.call("submit_task", bundle)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first, self.call("submit_task", bundle))
        task_id = first["data"]["task_id"]
        snapshot = self.state / "snapshots" / task_id
        self.assertTrue((snapshot / "README.txt").exists())
        self.assertTrue((snapshot / "CONTEXT.txt").exists())
        self.assertFalse((snapshot / "PRIVATE.txt").exists())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": task_id})
            if item["data"]["task"]["status"] == "review_required":
                break
            time.sleep(0.02)
        self.assertEqual(item["data"]["task"]["status"], "review_required", item)
        self.assertIn("Done", item["data"]["task"]["result"]["conclusion"])
        self.assertEqual(item["data"]["task"]["request"]["acceptance"], ["README inspected"])
        self.assertEqual(item["data"]["task"]["request"]["scope"], ["README.txt"])
        found = self.call("list_tasks", {})
        self.assertEqual(found["data"]["tasks"][0]["id"], task_id)
        complete = self.call("complete_task", {"task_id": task_id,
                             "review_note": "README inspected",
                             "request_id": uuid.uuid4().hex})
        self.assertEqual(complete["data"]["status"], "completed")
        self.assertEqual(self.call("list_tasks", {})["data"]["tasks"], [])

    def test_rejects_scope_expansion(self) -> None:
        result = self.call("submit_task", {
            "project_id": "sample", "objective": "Read private file",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["PRIVATE.txt"], "request_id": uuid.uuid4().hex})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PROJECT_NOT_ALLOWED")

    def test_edit_action_fails_closed(self) -> None:
        result = self.call("submit_task", {
            "project_id": "sample", "objective": "Edit a file",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["README.txt"], "actions": ["write"],
            "request_id": uuid.uuid4().hex})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "SANDBOX_UNAVAILABLE")

    def test_native_write_requires_project_opt_in(self) -> None:
        result = self.call("submit_task", {
            "project_id": "sample", "objective": "Edit a file",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["README.txt"], "actions": ["read", "write", "execute"],
            "request_id": uuid.uuid4().hex})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "SANDBOX_UNAVAILABLE")

    def _enable_write_backend(self, paths: list[str]) -> None:
        for command in (["init", "-q"], ["config", "user.name", "Test User"],
                        ["config", "user.email", "test@example.invalid"], ["add", "."],
                        ["commit", "-qm", "baseline"]):
            subprocess.run(["git", "-C", str(self.root), *command], check=True)
        config = json.loads(self.config.read_text())
        config["projects"]["sample"]["write_backend"] = {
            "enabled": True, "write_paths": paths}
        self.config.write_text(json.dumps(config))
        self.config.chmod(0o600)

    def test_native_write_task_commits_on_isolated_branch(self) -> None:
        self._enable_write_backend(["README.txt"])
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, subprocess, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --max-budget-usd --strict-mcp-config --resume'); sys.exit(0)\n"
            "pathlib.Path('README.txt').write_text('edited by CC')\n"
            "subprocess.run(['git','add','README.txt'],check=True)\n"
            "subprocess.run(['git','commit','-qm','implement task'],check=True)\n"
            "print(json.dumps({'type':'result','session_id':'write-session',"
            "'result':'Implemented and tested','is_error':False,'total_cost_usd':0.01}),flush=True)\n",
            encoding="utf-8")
        health = self.call("doctor", {})
        self.assertTrue(health["data"]["native_write"]["projects"]["sample"]["ready"])
        params = {"project_id": "sample", "objective": "Edit README", "context": "",
                  "acceptance": ["README changed"], "deliverables": ["Local commit"],
                  "scope": ["README.txt"], "actions": ["read", "write", "execute"],
                  "request_id": uuid.uuid4().hex}
        first = self.call("submit_task", params)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first, self.call("submit_task", params))
        task_id = first["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if item["status"] == "review_required":
                break
            time.sleep(0.02)
        self.assertEqual(item["status"], "review_required", item)
        workspace = item["result"]["workspace"]
        self.assertEqual(workspace["changed_paths"], ["README.txt"])
        self.assertEqual(workspace["outside_scope"], [])
        self.assertEqual(len(workspace["commits"]), 1)
        self.assertFalse(workspace["dirty"])
        self.assertEqual((self.root / "README.txt").read_text(), "public task content")
        self.assertEqual((Path(workspace["worktree_path"]) / "README.txt").read_text(), "edited by CC")

    def test_native_write_reports_changes_outside_declared_scope(self) -> None:
        self._enable_write_backend(["README.txt", "PRIVATE.txt"])
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --max-budget-usd --strict-mcp-config --resume'); sys.exit(0)\n"
            "pathlib.Path('PRIVATE.txt').write_text('changed')\n"
            "print(json.dumps({'type':'result','session_id':'write-session',"
            "'result':'Done','is_error':False,'total_cost_usd':0.01}),flush=True)\n",
            encoding="utf-8")
        first = self.call("submit_task", {
            "project_id": "sample", "objective": "Edit README", "context": "",
            "acceptance": ["README changed"], "deliverables": ["Diff"],
            "scope": ["README.txt"], "actions": ["read", "write", "execute"],
            "request_id": uuid.uuid4().hex})
        self.assertTrue(first["ok"], first)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": first["data"]["task_id"]})["data"]["task"]
            if item["status"] == "review_required":
                break
            time.sleep(0.02)
        self.assertEqual(item["status"], "review_required", item)
        self.assertEqual(item["result"]["workspace"]["outside_scope"], ["PRIVATE.txt"])

    def test_native_write_failure_preserves_partial_edit(self) -> None:
        self._enable_write_backend(["README.txt"])
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --max-budget-usd --strict-mcp-config --resume'); sys.exit(0)\n"
            "pathlib.Path('README.txt').write_text('partial change')\n"
            "sys.exit(3)\n", encoding="utf-8")
        first = self.call("submit_task", {
            "project_id": "sample", "objective": "Edit README", "context": "",
            "acceptance": ["README changed"], "deliverables": ["Diff"],
            "scope": ["README.txt"], "actions": ["read", "write", "execute"],
            "request_id": uuid.uuid4().hex})
        self.assertTrue(first["ok"], first)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": first["data"]["task_id"]})["data"]["task"]
            if item["status"] == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(item["status"], "failed", item)
        workspace = item["result"]["workspace"]
        self.assertEqual(workspace["changed_paths"], ["README.txt"])
        self.assertTrue(workspace["dirty"])
        self.assertEqual((self.root / "README.txt").read_text(), "public task content")
        reviewed = self.call("complete_task", {"task_id": first["data"]["task_id"],
                    "review_note": "Inspected retained diff; accepted despite CLI exit error",
                    "request_id": uuid.uuid4().hex})
        self.assertEqual(reviewed["data"]["status"], "completed")

    def test_symlink_scope_fails_closed(self) -> None:
        (self.root / "linked.txt").symlink_to(self.root / "PRIVATE.txt")
        config = json.loads(self.config.read_text())
        config["projects"]["sample"]["read_paths"].append("linked.txt")
        self.config.write_text(json.dumps(config))
        self.config.chmod(0o600)
        result = self.call("submit_task", {
            "project_id": "sample", "objective": "Read linked file",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["linked.txt"], "request_id": uuid.uuid4().hex})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "PROJECT_NOT_ALLOWED")

    def test_blocking_question_roundtrip(self) -> None:
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, socket, time\n"
            "time.sleep(0.1)\n"
            "sock=socket.socket(socket.AF_UNIX)\n"
            "sock.connect(os.environ['CODEX1CC_STATE_DIR']+'/daemon.sock')\n"
            "request={'method':'ask_question','params':{'task_id':os.environ['CODEX1CC_TASK_ID'],"
            "'text':'Which label?','options':['A','B']}}\n"
            "sock.sendall((json.dumps(request)+'\\n').encode())\n"
            "answer=json.loads(sock.makefile('r').readline())['data']['answer']\n"
            "print(json.dumps({'type':'result','session_id':'fake-question-session',"
            "'result':'Selected '+answer,'is_error':False,'total_cost_usd':0.001}),flush=True)\n",
            encoding="utf-8",
        )
        bundle = {"project_id": "sample", "objective": "Ask one question",
                  "context": "", "acceptance": ["Selection recorded"],
                  "deliverables": ["Selection"], "scope": ["README.txt"],
                  "request_id": uuid.uuid4().hex}
        first = self.call("submit_task", bundle)
        self.assertTrue(first["ok"], first)
        task_id = first["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": task_id})["data"]
            if item["task"]["status"] == "waiting_answer":
                break
            time.sleep(0.02)
        self.assertEqual(item["task"]["status"], "waiting_answer", item)
        question_id = item["questions"][0]["id"]
        answer = self.call("respond_task", {"task_id": task_id, "question_id": question_id,
                           "answer": "B", "request_id": uuid.uuid4().hex})
        self.assertTrue(answer["ok"], answer)
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": task_id})["data"]
            if item["task"]["status"] == "review_required":
                break
            time.sleep(0.02)
        self.assertEqual(item["task"]["result"]["conclusion"], "Selected B")

    def test_cancel_active_process(self) -> None:
        self.cli.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        first = self.call("submit_task", {
            "project_id": "sample", "objective": "Slow test", "context": "",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex})
        self.assertTrue(first["ok"], first)
        task_id = first["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if item["status"] == "running":
                break
            time.sleep(0.02)
        self.assertEqual(item["status"], "running")
        canceled = self.call("cancel_task", {"task_id": task_id, "request_id": uuid.uuid4().hex})
        self.assertEqual(canceled["data"]["status"], "canceled")
        self.assertEqual(self.call("get_task", {"task_id": task_id})["data"]["task"]["status"], "canceled")

    def test_shutdown_without_active_tasks(self) -> None:
        result = self.call("shutdown", {})
        self.assertTrue(result["ok"], result)
        self.daemon.wait(timeout=5)
        self.assertFalse(self.socket.exists())

    def test_retention_prunes_only_tool_owned_history(self) -> None:
        first = self.call("submit_task", {
            "project_id": "sample", "objective": "Read public file", "context": "",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex})
        task_id = first["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if item["status"] == "review_required":
                break
            time.sleep(0.02)
        self.assertEqual(item["status"], "review_required")
        self.call("complete_task", {"task_id": task_id, "review_note": "checked",
                                   "request_id": uuid.uuid4().hex})
        self.call("shutdown", {})
        self.daemon.wait(timeout=5)
        database = sqlite3.connect(self.state / "state.sqlite3")
        database.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time() - 31 * 86400, task_id))
        database.commit()
        database.close()
        from codex1cc import store as store_module
        original_state = store_module.STATE
        try:
            store_module.STATE = self.state
            store = store_module.Store(self.state / "state.sqlite3")
            self.assertEqual(store.prune_terminal_history(), 1)
            self.assertIsNone(store.one(task_id)["result"])
            store.db.close()
        finally:
            store_module.STATE = original_state
        self.assertFalse((self.state / "snapshots" / task_id).exists())
        self.assertTrue((self.root / "README.txt").exists())


if __name__ == "__main__":
    unittest.main()
