"""Upgrading never shuts down a running worker and refreshes tools after restart."""

import unittest
from unittest import mock

from codex1cc.upgrade import upgrade_executor


class UpgradeTest(unittest.IsolatedAsyncioTestCase):
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
