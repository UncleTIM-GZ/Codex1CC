"""App Server adapter tests against a local protocol peer; no model calls."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from websockets.asyncio.server import unix_serve

from codex1cc.handoff_host import AppServerHost, HostError


class ProtocolPeer:
    def __init__(self):
        self.mode = "legacy"
        self.status = "idle"
        self.turns = []
        self.clients = set()
        self.starts = 0
        self.last_turn_params = None
        self.include_ack = True
        self.cwd = "/tmp/demo"
        self.approval_tool = None
        self.approval_task_id = None
        self.approval_response = None
        self.approval_received = asyncio.Event()
        self.approval_during_start = False
        self.foreign_unload = False
        self.foreign_approval = False

    def approval_request(self, ident):
        return {"id": ident, "method": "mcpServer/elicitation/request",
                "params": {"threadId": "saved-thread", "serverName": "codex1cc", "mode": "form",
                           "requestedSchema": {"type": "object", "properties": {}},
                           "message": f'Allow the codex1cc MCP server to run tool "{self.approval_tool}"?',
                           "_meta": {"codex_approval_kind": "mcp_tool_call",
                                     "tool_params": {"task_id": self.approval_task_id}}}}

    async def handler(self, socket):
        self.clients.add(socket)
        try:
            async for raw in socket:
                message = json.loads(raw)
                ident = message.get("id")
                if ident is None:
                    continue
                if "method" not in message:
                    self.approval_response = message
                    self.approval_received.set()
                    continue
                method = message["method"]
                params = message.get("params") or {}
                if method == "initialize":
                    result = {}
                elif method == "thread/start":
                    self.cwd = params["cwd"]
                    result = {"thread": {"id": "saved-thread"}}
                elif method == "thread/read":
                    thread = {
                        "id": "saved-thread", "historyMode": self.mode,
                        "path": "/private/saved.jsonl", "status": {"type": self.status},
                        "canAcceptDirectInput": True, "cwd": self.cwd,
                    }
                    if params.get("includeTurns"):
                        thread["turns"] = list(self.turns)
                    result = {"thread": thread}
                elif method == "thread/resume":
                    if self.foreign_approval:
                        request = self.approval_request(9010)
                        request["params"]["threadId"] = "unrelated-thread"
                        await socket.send(json.dumps(request))
                    if self.foreign_unload:
                        await socket.send(json.dumps({"method": "thread/status/changed", "params": {
                            "threadId": "unrelated-thread", "status": {"type": "notLoaded"}}}))
                    result = {"thread": {"id": "saved-thread"}}
                elif method == "mcpServerStatus/list":
                    names = ["get_task", "respond_task", "continue_task", "complete_task", "manage_goal"]
                    if self.include_ack:
                        names.append("ack_handoff")
                    result = {"data": [{"name": "codex1cc", "runtimeStatus": "connected",
                                        "tools": {name: {} for name in names}}], "nextCursor": None}
                elif method == "turn/start":
                    self.starts += 1
                    self.last_turn_params = params
                    turn = {"id": f"turn-{self.starts}", "status": "inProgress",
                            "items": [{"type": "userMessage", "content": params["input"]}]}
                    self.turns.append(turn)
                    self.status = "active"
                    result = {"turn": turn}
                    asyncio.create_task(self.finish(turn))
                    if self.approval_during_start:
                        await socket.send(json.dumps(self.approval_request(ident)))
                else:
                    await socket.send(json.dumps({"id": ident, "error": {"message": "unknown method"}}))
                    continue
                await socket.send(json.dumps({"id": ident, "result": result}))
        finally:
            self.clients.discard(socket)

    async def finish(self, turn):
        await asyncio.sleep(0.15)
        if self.approval_tool and not self.approval_during_start:
            socket = next(iter(self.clients))
            await socket.send(json.dumps(self.approval_request(9001)))
            await asyncio.wait_for(self.approval_received.wait(), 1)
        turn["status"] = "completed"
        turn["items"].append({"type": "agentMessage", "text": "DONE"})
        self.status = "idle"
        for socket in list(self.clients):
            await socket.send(json.dumps({"method": "turn/completed", "params": {
                "threadId": "saved-thread", "turn": turn}}))
            await socket.send(json.dumps({"method": "thread/status/changed", "params": {
                "threadId": "saved-thread", "status": {"type": "idle"}}}))


class HandoffHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.chmod(self.temp.name, 0o700)
        self.socket_path = str(Path(self.temp.name) / "host.sock")
        self.peer = ProtocolPeer()
        self.server = await unix_serve(self.peer.handler, self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.host = AppServerHost(socket_path=self.socket_path)
        self.binding = {"thread_id": "saved-thread"}

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.temp.cleanup()

    async def test_probe_requires_resumable_history_and_existing_session(self):
        good = await self.host.probe(self.binding)
        self.assertTrue(good.connected)
        self.assertEqual(good.status, "idle")
        self.peer.mode = "paginated"
        rejected = await self.host.probe(self.binding)
        self.assertFalse(rejected.connected)
        self.assertIn("legacy", rejected.reason)

    async def test_probe_rejects_missing_acknowledgement_tool(self):
        self.peer.include_ack = False
        rejected = await self.host.probe(self.binding)
        self.assertFalse(rejected.connected)
        self.assertIn("ack_handoff", rejected.reason)

    async def test_delivery_completion_reconcile_and_duplicate_marker(self):
        started = await self.host.deliver(self.binding, "event-1", "Inspect the task")
        self.assertEqual(started.status, "inProgress")
        self.assertEqual(self.peer.last_turn_params["sandboxPolicy"], {"type": "readOnly"})
        self.assertEqual(self.peer.last_turn_params["approvalPolicy"], "on-request")
        finished = await self.host.wait_for_turn(self.binding, started.turn_id, timeout=2)
        self.assertEqual((finished.status, finished.result), ("completed", "DONE"))
        recorded = await self.host.reconcile(self.binding, started.turn_id)
        self.assertEqual(recorded.status, "completed")
        duplicate = await self.host.deliver(self.binding, "event-1", "Inspect the task")
        self.assertEqual(duplicate.turn_id, started.turn_id)
        self.assertEqual(self.peer.starts, 1)

    async def test_foreign_thread_unload_does_not_stop_controller_observation(self):
        self.peer.foreign_unload = True
        started = await self.host.deliver(self.binding, "event-foreign", "Inspect the task")
        result = await self.host.wait_for_turn(self.binding, started.turn_id, timeout=2)
        self.assertEqual(result.status, "completed")

    async def test_foreign_thread_approval_is_not_declined_by_this_controller(self):
        self.peer.foreign_approval = True
        started = await self.host.deliver(self.binding, "event-foreign-approval", "Inspect the task")
        result = await self.host.wait_for_turn(self.binding, started.turn_id, timeout=2)
        self.assertEqual(result.status, "completed")
        self.assertIsNone(self.peer.approval_response)

    async def test_approval_is_scoped_to_current_task_even_before_waiter_attaches(self):
        self.peer.approval_tool = "get_task"
        self.peer.approval_task_id = "task-1"
        binding = {**self.binding, "_handoff_task_id": "task-1",
                   "_handoff_event_id": "event-4", "_handoff_receipt_token": "secret"}
        started = await self.host.deliver(binding, "event-4", "Inspect the task")
        await asyncio.wait_for(self.peer.approval_received.wait(), 1)
        self.assertEqual(self.peer.approval_response["result"]["action"], "accept")
        self.assertEqual((await self.host.wait_for_turn(binding, started.turn_id, timeout=2)).status, "completed")

    async def test_approval_rejects_other_tasks(self):
        self.peer.approval_tool = "get_task"
        self.peer.approval_task_id = "other-task"
        binding = {**self.binding, "_handoff_task_id": "task-1",
                   "_handoff_event_id": "event-5", "_handoff_receipt_token": "secret"}
        started = await self.host.deliver(binding, "event-5", "Inspect the task")
        await asyncio.wait_for(self.peer.approval_received.wait(), 1)
        self.assertEqual(self.peer.approval_response["result"]["action"], "decline")
        self.assertEqual((await self.host.wait_for_turn(binding, started.turn_id, timeout=2)).status, "completed")

    async def test_server_request_id_can_match_pending_client_request(self):
        self.peer.approval_tool = "get_task"
        self.peer.approval_task_id = "task-1"
        self.peer.approval_during_start = True
        binding = {**self.binding, "_handoff_task_id": "task-1",
                   "_handoff_event_id": "event-6", "_handoff_receipt_token": "secret"}
        started = await self.host.deliver(binding, "event-6", "Inspect the task")
        await asyncio.wait_for(self.peer.approval_received.wait(), 1)
        self.assertEqual(self.peer.approval_response["result"]["action"], "accept")
        self.assertEqual((await self.host.wait_for_turn(binding, started.turn_id, timeout=2)).status, "completed")

    async def test_goal_management_is_automatically_approved_for_delivered_task(self):
        self.peer.approval_tool = "manage_goal"
        self.peer.approval_task_id = "task-1"
        binding = {**self.binding, "_handoff_task_id": "task-1",
                   "_handoff_event_id": "event-goal", "_handoff_receipt_token": "secret"}
        started = await self.host.deliver(binding, "event-goal", "Manage the goal")
        await asyncio.wait_for(self.peer.approval_received.wait(), 1)
        self.assertEqual(self.peer.approval_response["result"]["action"], "accept")
        self.assertEqual((await self.host.wait_for_turn(binding, started.turn_id, timeout=2)).status, "completed")

    async def test_busy_waits_for_idle_without_model_request(self):
        started = await self.host.deliver(self.binding, "event-2", "Inspect the task")
        busy = await self.host.deliver(self.binding, "event-3", "Inspect the task")
        self.assertEqual(busy.status, "busy")
        ready = await self.host.wait_for_idle(self.binding, timeout=2)
        self.assertTrue(ready.connected)
        self.assertEqual(ready.status, "idle")
        self.assertEqual(self.peer.starts, 1)
        self.assertEqual((await self.host.reconcile(self.binding, started.turn_id)).status, "completed")

    async def test_empty_thread_creation_fails_closed(self):
        with self.assertRaisesRegex(HostError, "not persistent"):
            await self.host.create_thread(self.temp.name)

    async def test_explicit_initialization_waits_for_persisted_turn(self):
        ident = await self.host.initialize_thread(self.temp.name, timeout=2)
        self.assertEqual(ident, "saved-thread")
        self.assertEqual(self.peer.starts, 1)
        self.assertEqual(self.peer.turns[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
