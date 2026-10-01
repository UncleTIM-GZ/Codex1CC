"""Upgrading never shuts down a running worker and refreshes tools after restart."""

import unittest
import os
import sqlite3
from pathlib import Path
import tempfile
from unittest import mock

from codex1cc.upgrade import upgrade_executor, _worker_still_alive
from codex1cc.common import BridgeError
from codex1cc.handoff_host import HostError


class UpgradeTest(unittest.IsolatedAsyncioTestCase):
    async def test_saved_live_pid_blocks_upgrade_even_for_terminal_task(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory) / "state.sqlite3") as db:
                db.execute("CREATE TABLE tasks(id TEXT,process_id INTEGER,status TEXT)")
                db.execute("INSERT INTO tasks VALUES('task',?,'failed')", (os.getpid(),))
            with mock.patch("codex1cc.upgrade.STATE", Path(directory)):
                self.assertTrue(_worker_still_alive("task"))
                self.assertFalse(_worker_still_alive("missing"))

    async def test_mcp_reload_failure_is_not_reported_as_success(self):
        rpc = mock.AsyncMock(side_effect=[{"tasks": []}, {"stopping": True}, {"protocol_version": 3}])
        with mock.patch("codex1cc.upgrade.rpc", rpc), \
             mock.patch("codex1cc.upgrade._worker_still_alive", return_value=False), \
             mock.patch("codex1cc.upgrade.SOCKET") as socket, \
             mock.patch("codex1cc.upgrade.install_skill"), \
             mock.patch("codex1cc.upgrade.AppServerHost") as host:
            socket.exists.return_value = False
            host.return_value._socket_path = mock.AsyncMock(side_effect=HostError("offline"))
            with self.assertRaises(BridgeError) as error:
                await upgrade_executor()
            self.assertEqual(error.exception.code, "HANDOFF_UNAVAILABLE")

    async def test_waits_for_acknowledged_controller_on_second_page(self):
        finished = {"id": "old", "status": "completed", "handoff": {"counts": {}}}
        pending = {"id": "task", "status": "review_required", "handoff": {
            "counts": {"handled": 1}, "latest": {"status": "handled", "turn_id": "turn", "codex_status": None}}}
        replies = [{"tasks": [finished] * 100}, {"tasks": [pending]}, {}, {"tasks": []},
                   {"stopping": True, "instance_id": "old"},
                   {"protocol_version": 3, "instance_id": "replacement"}, {"protocol_version": 3}]
        rpc = mock.AsyncMock(side_effect=replies)
        connection = mock.AsyncMock()
        connection.__aenter__.return_value = connection
        with mock.patch("codex1cc.upgrade.rpc", rpc), \
             mock.patch("codex1cc.upgrade._worker_still_alive", return_value=False), \
             mock.patch("codex1cc.upgrade._event_cursor", return_value=10), \
             mock.patch("codex1cc.upgrade.SOCKET") as socket, \
             mock.patch("codex1cc.upgrade.asyncio.sleep", mock.AsyncMock()), \
             mock.patch("codex1cc.upgrade.install_skill"), \
             mock.patch("codex1cc.upgrade.AppServerHost") as host, \
             mock.patch("codex1cc.upgrade._Connection", return_value=connection), \
             mock.patch("builtins.print"):
            socket.exists.return_value = True
            host.return_value._socket_path = mock.AsyncMock(return_value="/socket")
            await upgrade_executor()
        self.assertEqual(rpc.call_args_list[1].args[1]["offset"], 100)
        self.assertEqual(rpc.call_args_list[2].args[0], "wait_task")
        self.assertEqual(rpc.call_args_list[2].args[1]["task_id"], "task")

    async def test_drains_worker_before_restart_and_adopts_retained_failed_goal(self):
        replies = [
            {"tasks": [{"id": "task", "status": "running", "handoff": {"counts": {}}}]},
            TimeoutError(),
            {"tasks": []}, {"stopping": True}, {"protocol_version": 3},
            {"task": {"status": "failed"}}, {"goal": {"status": "active"}}, {"announced": True},
        ]
        rpc = mock.AsyncMock(side_effect=replies)
        connection = mock.AsyncMock()
        connection.__aenter__.return_value = connection
        with mock.patch("codex1cc.upgrade.rpc", rpc), \
             mock.patch("codex1cc.upgrade._worker_still_alive", return_value=False), \
             mock.patch("codex1cc.upgrade._event_cursor", return_value=0), \
             mock.patch("codex1cc.upgrade.SOCKET") as socket, \
             mock.patch("codex1cc.upgrade.asyncio.sleep", mock.AsyncMock()), \
             mock.patch("codex1cc.upgrade.install_skill") as skill, \
             mock.patch("codex1cc.upgrade.AppServerHost") as host, \
             mock.patch("codex1cc.upgrade._Connection", return_value=connection), \
             mock.patch("builtins.print"):
            socket.exists.return_value = False
            host.return_value._socket_path = mock.AsyncMock(return_value="/socket")
            await upgrade_executor("task")
        self.assertEqual([call.args[0] for call in rpc.call_args_list],
                         ["list_tasks", "wait_task", "list_tasks", "shutdown", "doctor", "get_task", "manage_goal", "announce_upgrade"])
        skill.assert_called_once_with(force=True)
        connection.request.assert_awaited_once_with("config/mcpServer/reload", {})
