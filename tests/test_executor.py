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

    def test_task_cannot_set_a_cc_cost_limit(self) -> None:
        result = self.call("submit_task", {
            "project_id": "sample", "objective": "Inspect README",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["README.txt"], "limits": {"usd": 0.85},
            "request_id": uuid.uuid4().hex})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

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
        self.assertEqual(health["data"]["parallel"]["projects"]["sample"], 3)
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

    def test_context_policy_reaches_claude_and_context_error_keeps_worktree(self) -> None:
        self._enable_write_backend(["README.txt"])
        config = json.loads(self.config.read_text())
        config["projects"]["sample"]["context_policy"] = {
            "auto_compact_window": 400000, "auto_compact_percent": 65}
        self.config.write_text(json.dumps(config))
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --max-budget-usd --strict-mcp-config --resume'); sys.exit(0)\n"
            "pathlib.Path('README.txt').write_text('partial work')\n"
            "pathlib.Path('context-policy.txt').write_text("
            "os.environ['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] + ',' + "
            "os.environ['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'])\n"
            "print('API Error: 400 maximum context length exceeded', file=sys.stderr)\n"
            "sys.exit(1)\n", encoding="utf-8")
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Edit README", "context": "",
            "acceptance": ["README changed"], "deliverables": ["Diff"],
            "scope": ["README.txt"], "actions": ["read", "write", "execute"],
            "request_id": uuid.uuid4().hex})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if item["status"] == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(item["exit_reason"], "CONTEXT_LIMIT", item)
        self.assertIn("fresh Claude session", item["result"]["next_action"])
        worktree = Path(item["result"]["workspace"]["worktree_path"])
        self.assertEqual((worktree / "README.txt").read_text(), "partial work")
        self.assertEqual((worktree / "context-policy.txt").read_text(), "400000,65")
        self.assertEqual((self.root / "README.txt").read_text(), "public task content")

    def test_invalid_context_policy_rejected_before_task_creation(self) -> None:
        config = json.loads(self.config.read_text())
        config["projects"]["sample"]["context_policy"] = {
            "auto_compact_window": 50000}
        self.config.write_text(json.dumps(config))
        result = self.call("submit_task", {
            "project_id": "sample", "objective": "Read README",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_CONFIG")

    def test_explicit_long_task_limit(self) -> None:
        config = json.loads(self.config.read_text())
        config["projects"]["sample"]["limits"]["seconds"] = 21600
        self.config.write_text(json.dumps(config))
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Long review",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["README.txt"], "limits": {"seconds": 14400},
            "request_id": uuid.uuid4().hex})
        self.assertTrue(started["ok"], started)
        task = self.call("get_task", {"task_id": started["data"]["task_id"]})["data"]["task"]
        self.assertEqual(task["request"]["limits"]["seconds"], 14400)

    def test_reviewed_write_task_can_relay_to_fresh_session_in_same_worktree(self) -> None:
        self._enable_write_backend(["README.txt"])
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --max-budget-usd --strict-mcp-config --resume'); sys.exit(0)\n"
            "path=pathlib.Path('README.txt')\n"
            "if path.read_text() == 'public task content':\n"
            " path.write_text('phase one')\n"
            " print(json.dumps({'type':'result','session_id':'old-session','result':'Phase one',"
            "'is_error':False,'total_cost_usd':0.01}),flush=True)\n"
            "else:\n"
            " path.write_text('phase two')\n"
            " print(json.dumps({'type':'result','session_id':'new-session',"
            "'result':'Fresh=' + str('--resume' not in sys.argv),"
            "'is_error':False,'total_cost_usd':0.02}),flush=True)\n",
            encoding="utf-8")
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Two phases", "context": "",
            "acceptance": ["Phase two done"], "deliverables": ["Diff"],
            "scope": ["README.txt"], "actions": ["read", "write", "execute"],
            "request_id": uuid.uuid4().hex})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            first = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if first["status"] == "review_required":
                break
            time.sleep(0.02)
        self.assertEqual(first["status"], "review_required", first)
        original_worktree = first["result"]["workspace"]["worktree_path"]
        continued = self.call("continue_task", {
            "task_id": task_id, "instruction": "Finish phase two", "fresh_session": True,
            "request_id": uuid.uuid4().hex})
        self.assertTrue(continued["ok"], continued)
        self.assertTrue(continued["data"]["fresh_session"])
        while time.monotonic() < deadline:
            second = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if second["status"] == "review_required" and second["round_no"] == 2:
                break
            time.sleep(0.02)
        self.assertEqual(second["status"], "review_required", second)
        self.assertEqual(second["result"]["conclusion"], "Fresh=True")
        self.assertEqual(second["session_id"], "new-session")
        self.assertEqual(second["result"]["workspace"]["worktree_path"], original_worktree)
        self.assertAlmostEqual(second["usage"]["total_cost_usd"], 0.03)

    def test_unknown_cost_does_not_block_fresh_write_session(self) -> None:
        self._enable_write_backend(["README.txt"])
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --strict-mcp-config --resume'); sys.exit(0)\n"
            "if '--max-budget-usd' in sys.argv: sys.exit(2)\n"
            "path=pathlib.Path('README.txt')\n"
            "phase='one' if path.read_text() == 'public task content' else 'two'\n"
            "path.write_text('phase ' + phase)\n"
            "print(json.dumps({'type':'result','session_id':'session-'+phase,"
            "'result':'phase '+phase,'is_error':False}),flush=True)\n",
            encoding="utf-8")
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Two phases", "context": "",
            "acceptance": ["Phase two done"], "deliverables": ["Diff"],
            "scope": ["README.txt"], "actions": ["read", "write", "execute"],
            "request_id": uuid.uuid4().hex})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            first = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if first["status"] == "review_required":
                break
            time.sleep(0.02)
        self.assertEqual(first["status"], "review_required", first)
        self.assertIsNone(first["usage"]["total_cost_usd"])
        continued = self.call("continue_task", {
            "task_id": task_id, "instruction": "Finish phase two", "fresh_session": True,
            "request_id": uuid.uuid4().hex})
        self.assertTrue(continued["ok"], continued)
        while time.monotonic() < deadline:
            second = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if second["status"] == "review_required" and second["round_no"] == 2:
                break
            time.sleep(0.02)
        self.assertEqual(second["status"], "review_required", second)
        self.assertEqual(second["result"]["conclusion"], "phase two")

    def test_fresh_session_relay_rejects_read_only_task(self) -> None:
        started = self.call("submit_task", {
            "project_id": "sample", "objective": "Read README",
            "acceptance": ["done"], "deliverables": ["result"],
            "scope": ["README.txt"], "request_id": uuid.uuid4().hex})
        self.assertTrue(started["ok"], started)
        task_id = started["data"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.call("get_task", {"task_id": task_id})["data"]["task"]
            if task["status"] == "review_required":
                break
            time.sleep(0.02)
        self.assertEqual(task["status"], "review_required", task)
        refused = self.call("continue_task", {
            "task_id": task_id, "instruction": "Try to relay", "fresh_session": True,
            "request_id": uuid.uuid4().hex})
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "INVALID_STATE")

    def test_independent_tasks_run_in_parallel_but_conflicts_remain_serial(self) -> None:
        config = json.loads(self.config.read_text())
        config["projects"]["sample"]["read_paths"].extend(["PRIVATE.txt", "CONTEXT.txt"])
        config["projects"]["sample"]["parallel"] = {"max_agents": 2}
        self.config.write_text(json.dumps(config))
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, time\n"
            "time.sleep(0.8)\n"
            "print(json.dumps({'type':'result','session_id':'independent-session',"
            "'result':'Done','is_error':False,'total_cost_usd':0.01}),flush=True)\n",
            encoding="utf-8")

        def submit(scope: str, parallel_ok: bool) -> dict:
            return self.call("submit_task", {
                "project_id": "sample", "objective": "Inspect " + scope,
                "acceptance": ["done"], "deliverables": ["result"],
                "scope": [scope], "parallel_ok": parallel_ok,
                "request_id": uuid.uuid4().hex})

        first = submit("README.txt", True)
        second = submit("PRIVATE.txt", True)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["data"]["task_id"], second["data"]["task_id"])
        running_deadline = time.monotonic() + 0.5
        while time.monotonic() < running_deadline:
            current = [self.call("get_task", {"task_id": task["data"]["task_id"]})
                       ["data"]["task"]["status"] for task in (first, second)]
            if current == ["running", "running"]:
                break
            time.sleep(0.02)
        self.assertEqual(current, ["running", "running"])
        conflict = submit("README.txt", True)
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "PROJECT_BUSY")
        at_limit = submit("CONTEXT.txt", True)
        self.assertFalse(at_limit["ok"])
        self.assertEqual(at_limit["error"]["code"], "PROJECT_BUSY")
        serial = submit("CONTEXT.txt", False)
        self.assertFalse(serial["ok"])
        self.assertEqual(serial["error"]["code"], "PROJECT_BUSY")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            states = [self.call("get_task", {"task_id": task["data"]["task_id"]})
                      ["data"]["task"]["status"] for task in (first, second)]
            if states == ["review_required", "review_required"]:
                break
            time.sleep(0.02)
        self.assertEqual(states, ["review_required", "review_required"])

    def test_parallel_scope_conflicts_include_parent_paths(self) -> None:
        from codex1cc.daemon import Executor

        self.assertTrue(Executor._scope_conflict(["src"], ["src/module.py"]))
        self.assertTrue(Executor._scope_conflict(["src/module.py"], ["src"]))
        self.assertTrue(Executor._scope_conflict(["."], ["docs/guide.md"]))
        self.assertFalse(Executor._scope_conflict(["src"], ["src2/module.py"]))

    def test_parallel_write_agents_use_separate_worktrees(self) -> None:
        self._enable_write_backend(["README.txt", "PRIVATE.txt"])
        config = json.loads(self.config.read_text())
        config["projects"]["sample"]["parallel"] = {"max_agents": 2}
        self.config.write_text(json.dumps(config))
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys, time\n"
            "if '--help' in sys.argv:\n"
            " print('--print --output-format --permission-mode --tools --max-budget-usd --strict-mcp-config --resume'); sys.exit(0)\n"
            "name='README.txt' if 'README.txt' in sys.argv[-1] else 'PRIVATE.txt'\n"
            "pathlib.Path(name).write_text('edited ' + name)\n"
            "time.sleep(0.6)\n"
            "print(json.dumps({'type':'result','session_id':'session-'+name,"
            "'result':'Done','is_error':False,'total_cost_usd':0.01}),flush=True)\n",
            encoding="utf-8")

        def submit(scope: str) -> dict:
            return self.call("submit_task", {
                "project_id": "sample", "objective": "Edit " + scope, "context": "",
                "acceptance": [scope + " changed"], "deliverables": ["Diff"],
                "scope": [scope], "actions": ["read", "write", "execute"],
                "parallel_ok": True, "request_id": uuid.uuid4().hex})

        first, second = submit("README.txt"), submit("PRIVATE.txt")
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual(submit("README.txt")["error"]["code"], "PROJECT_BUSY")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            tasks = [self.call("get_task", {"task_id": response["data"]["task_id"]})
                     ["data"]["task"] for response in (first, second)]
            if all(task["status"] == "review_required" for task in tasks):
                break
            time.sleep(0.02)
        self.assertTrue(all(task["status"] == "review_required" for task in tasks), tasks)
        worktrees = [task["result"]["workspace"]["worktree_path"] for task in tasks]
        self.assertNotEqual(*worktrees)
        self.assertEqual(tasks[0]["result"]["workspace"]["changed_paths"], ["README.txt"])
        self.assertEqual(tasks[1]["result"]["workspace"]["changed_paths"], ["PRIVATE.txt"])
        self.assertEqual((self.root / "README.txt").read_text(), "public task content")
        self.assertEqual((self.root / "PRIVATE.txt").read_text(), "private marker")

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
