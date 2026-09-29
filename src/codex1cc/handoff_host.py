"""Event-driven connection to a separately managed Codex App Server daemon.

Closing the WebSocket does not stop a turn. The caller persists turn IDs before
waiting and reconciles uncertain starts by event marker before retrying.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed


_EVENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MAX_MESSAGE = 8 * 1024 * 1024


class HostError(Exception):
    """A host connection or protocol failure; never implies safe redelivery."""


@dataclass(frozen=True)
class Capability:
    connected: bool
    reason: str = ""
    thread_id: str | None = None
    status: str = "unknown"


@dataclass(frozen=True)
class Delivery:
    turn_id: str | None
    status: str
    result: str | None = None
    error: str | None = None


def _thread_id(binding: dict[str, Any]) -> str:
    value = binding.get("thread_id") if isinstance(binding, dict) else None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise HostError("A registered Codex thread_id is required")
    return value


def _turn_result(turn: dict[str, Any]) -> Delivery:
    pieces: list[str] = []
    for item in turn.get("items") or []:
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            message = item.get("text")
            if isinstance(message, str) and message:
                pieces.append(message)
    failure = turn.get("error")
    error = failure.get("message") if isinstance(failure, dict) else None
    return Delivery(turn.get("id"), turn.get("status", "unknown"), "\n".join(pieces) or None, error)


class _Connection:
    def __init__(self, socket_path: str, approval_context: dict[str, str] | None = None):
        path = Path(socket_path)
        if not path.is_absolute():
            raise HostError("Codex App Server socket path must be absolute")
        try:
            directory = path.parent.stat()
            link = path.lstat()
            resolved = path.resolve(strict=True)
            target_directory = resolved.parent.stat()
            info = resolved.stat()
        except OSError as exc:
            raise HostError(f"Codex App Server socket unavailable: {exc}") from exc
        owner = os.getuid()
        if any(data.st_uid != owner or data.st_mode & 0o077 for data in (directory, target_directory)):
            raise HostError("Codex App Server socket directory owner or permissions are invalid")
        if link.st_uid != owner or not (stat.S_ISLNK(link.st_mode) or stat.S_ISSOCK(link.st_mode)):
            raise HostError("Codex App Server socket link owner or type is invalid")
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077:
            raise HostError("Codex App Server socket type or owner is invalid")
        self.socket_path = str(resolved)
        self.approval_context = approval_context or {}
        self.socket: Any = None
        self.reader_task: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self.sequence = 0

    async def __aenter__(self) -> "_Connection":
        try:
            self.socket = await asyncio.wait_for(
                unix_connect(self.socket_path, uri="ws://localhost", max_size=_MAX_MESSAGE), 5
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise HostError(f"Codex App Server unavailable: {exc}") from exc
        self.reader_task = asyncio.create_task(self._read())
        try:
            await self.request("initialize", {
                "clientInfo": {"name": "codex1cc", "title": "Codex1CC", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            }, timeout=5)
            await self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_: object) -> None:
        if self.socket:
            await self.socket.close()
        if self.reader_task:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass

    async def send(self, message: dict[str, Any]) -> None:
        if not self.socket:
            raise HostError("Codex App Server connection is closed")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > _MAX_MESSAGE:
            raise HostError("Codex App Server message is too large")
        try:
            await self.socket.send(encoded)
        except ConnectionClosed as exc:
            raise HostError("Codex App Server disconnected") from exc

    async def request(self, method: str, params: dict[str, Any], *, timeout: float = 10) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        self.sequence += 1
        ident = self.sequence
        response = loop.create_future()
        self.pending[ident] = response
        try:
            await self.send({"id": ident, "method": method, "params": params})
            value = await asyncio.wait_for(response, timeout)
        except asyncio.TimeoutError as exc:
            raise HostError(f"Codex App Server {method} timed out; outcome is uncertain") from exc
        finally:
            self.pending.pop(ident, None)
        if not isinstance(value, dict):
            raise HostError(f"Codex App Server {method} returned invalid data")
        if "error" in value:
            error = value["error"]
            description = error.get("message") if isinstance(error, dict) else str(error)
            raise HostError(f"Codex App Server {method}: {description}")
        result = value.get("result")
        if not isinstance(result, dict):
            raise HostError(f"Codex App Server {method} returned no result")
        return result

    async def _read(self) -> None:
        assert self.socket
        try:
            async for raw in self.socket:
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if isinstance(message.get("id"), int) and isinstance(message.get("method"), str):
                    approved = self._allowed_approval(message)
                    if message["method"] == "mcpServer/elicitation/request":
                        answer = {"id": message["id"], "result": {
                            "action": "accept" if approved else "decline",
                            "content": {} if approved else None}}
                    else:
                        answer = {"id": message["id"], "error": {
                            "code": -32601, "message": "Interactive request unavailable in automatic handoff"}}
                    await self.socket.send(json.dumps(answer))
                    if not approved:
                        try:
                            self.messages.put_nowait({"method": "client/request_rejected",
                                                      "params": {"request_method": message["method"],
                                                                 "request_params": message.get("params")}})
                        except asyncio.QueueFull:
                            pass
                elif isinstance(message.get("id"), int):
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        future.set_result(message)
                elif message.get("method") in {"turn/completed", "thread/status/changed"}:
                    try:
                        self.messages.put_nowait(message)
                    except asyncio.QueueFull:
                        # A lost completion is reconciled from persisted history.
                        pass
        except (asyncio.CancelledError, ValueError, ConnectionClosed):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(HostError("Codex App Server disconnected"))
            try:
                self.messages.put_nowait({"method": "transport/disconnected"})
            except asyncio.QueueFull:
                pass

    def _allowed_approval(self, message: dict[str, Any]) -> bool:
        if message.get("method") != "mcpServer/elicitation/request":
            return False
        params = message.get("params") or {}
        meta = params.get("_meta") or {}
        if (params.get("serverName") != "codex1cc" or params.get("mode") != "form"
                or params.get("threadId") != self.approval_context.get("thread_id")
                or meta.get("codex_approval_kind") != "mcp_tool_call"
                or params.get("requestedSchema") != {"type": "object", "properties": {}}):
            return False
        task_id = self.approval_context.get("task_id")
        event_id = self.approval_context.get("event_id")
        token = self.approval_context.get("receipt_token")
        if not task_id or not event_id or not token:
            return False
        tool_args = meta.get("tool_params") or {}
        if not isinstance(tool_args, dict):
            return False
        message_text = params.get("message")
        for tool in ("get_task", "respond_task", "continue_task", "complete_task", "ack_handoff"):
            if message_text != f'Allow the codex1cc MCP server to run tool "{tool}"?':
                continue
            if tool == "ack_handoff":
                return tool_args.get("event_id") == event_id and tool_args.get("receipt_token") == token
            return tool_args.get("task_id") == task_id
        return False


class AppServerHost:
    """One-shot JSON-RPC client for the managed Codex App Server daemon.

    A dedicated persistent app-server thread can be explicitly initialized with
    ``initialize_thread`` and bound to the project. The managed host daemon is
    independently started by the user; this client never starts or stops it.
    """

    def __init__(self, socket_path: str | None = None,
                 command: tuple[str, ...] = ("codex", "app-server", "daemon", "version")):
        self.socket_path = socket_path
        self.command = command
        self.created_thread_id: str | None = None
        self._live_connections: dict[str, _Connection] = {}

    @staticmethod
    def _approval_context(binding: dict[str, Any]) -> dict[str, str]:
        context = {key: binding[f"_handoff_{key}"] for key in
                ("task_id", "event_id", "receipt_token")
                if isinstance(binding.get(f"_handoff_{key}"), str)}
        context["thread_id"] = _thread_id(binding)
        return context

    async def _socket_path(self) -> str:
        if self.socket_path is not None:
            return self.socket_path
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            raw, _ = await asyncio.wait_for(process.communicate(), 5)
            report = json.loads(raw)
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            raise HostError(f"Cannot inspect managed Codex App Server: {exc}") from exc
        if process.returncode or report.get("status") != "running":
            raise HostError("Managed Codex App Server is not running")
        path = report.get("socketPath")
        if not isinstance(path, str) or not path:
            raise HostError("Managed Codex App Server has no socket path")
        return path

    @staticmethod
    async def _check_handoff_tools(connection: _Connection, thread_id: str) -> None:
        cursor = None
        for _ in range(100):
            params: dict[str, Any] = {"threadId": thread_id, "detail": "toolsAndAuthOnly", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await connection.request("mcpServerStatus/list", params)
            for item in result.get("data") or []:
                if not isinstance(item, dict) or item.get("name") != "codex1cc":
                    continue
                tools = item.get("tools") or {}
                required = {"get_task", "respond_task", "continue_task", "complete_task", "ack_handoff"}
                if item.get("runtimeStatus") != "connected" or not isinstance(tools, dict):
                    raise HostError("Codex1CC MCP is unavailable in the bound Codex thread: " +
                                    str(item.get("toolsError") or item.get("runtimeStatus")))
                missing = required - set(tools)
                if missing:
                    raise HostError("Bound Codex thread lacks Codex1CC MCP tools: " + ", ".join(sorted(missing)))
                return
            cursor = result.get("nextCursor")
            if not cursor:
                break
        raise HostError("Bound Codex thread has no Codex1CC MCP connection")

    async def create_thread(self, cwd: str) -> str:
        raise HostError("An empty Codex thread is not persistent; bind an existing saved session")

    async def initialize_thread(self, cwd: str, *, timeout: float = 120) -> str:
        """Explicit, billable initialization of a resumable legacy thread."""
        path = Path(cwd).resolve()
        if not path.is_dir():
            raise HostError("Codex project directory does not exist")
        deadline = asyncio.get_running_loop().time() + timeout
        async with _Connection(await self._socket_path()) as connection:
            result = await connection.request("thread/start", {
                "cwd": str(path), "serviceName": "codex1cc", "historyMode": "legacy",
            })
            thread = result.get("thread")
            ident = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(ident, str) or not ident:
                raise HostError("Codex App Server did not return a thread id")
            self.created_thread_id = ident
            started = await connection.request("turn/start", {
                "threadId": ident,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
                "input": [{"type": "text", "text":
                           "Codex1CC initialization. Reply exactly READY. Do not use tools."}],
            }, timeout=30)
            turn = started.get("turn") or {}
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise HostError("Codex initialization turn has no id; outcome is uncertain")
            while True:
                try:
                    message = await asyncio.wait_for(
                        connection.messages.get(), max(0, deadline - asyncio.get_running_loop().time()))
                except asyncio.TimeoutError as exc:
                    raise HostError("Codex initialization turn did not complete in time") from exc
                if message.get("method") == "transport/disconnected":
                    raise HostError("Codex disconnected during initialization; outcome is uncertain")
                if message.get("method") != "turn/completed":
                    continue
                params = message.get("params") or {}
                completed = params.get("turn") or {}
                if params.get("threadId") == ident and completed.get("id") == turn_id:
                    if completed.get("status") != "completed":
                        raise HostError(f"Codex initialization ended: {completed.get('status')}")
                    break
        capability = await self.probe({"thread_id": ident, "cwd": str(path)})
        if not capability.connected:
            raise HostError(f"Codex initialization did not persist a resumable session: {capability.reason}")
        return ident

    async def probe(self, binding: dict[str, Any]) -> Capability:
        try:
            ident = _thread_id(binding)
            async with _Connection(await self._socket_path()) as connection:
                result = await connection.request("thread/read", {"threadId": ident})
                thread = result.get("thread")
                if not isinstance(thread, dict) or thread.get("id") != ident:
                    return Capability(False, "Codex thread was not found", ident)
                if thread.get("historyMode") != "legacy" or not thread.get("path"):
                    return Capability(False, "Codex thread does not have resumable legacy history", ident)
                if thread.get("canAcceptDirectInput") is False:
                    return Capability(False, "Codex thread cannot accept direct input", ident)
                expected_cwd = binding.get("cwd") or binding.get("project_root")
                if expected_cwd and Path(thread.get("cwd", "")).resolve() != Path(expected_cwd).resolve():
                    return Capability(False, "Codex thread belongs to a different project directory", ident)
                await connection.request("thread/resume", {"threadId": ident})
                await self._check_handoff_tools(connection, ident)
                status = thread.get("status") or {}
                status_type = status.get("type", "unknown") if isinstance(status, dict) else "unknown"
                if status_type in {"idle", "notLoaded", "active"}:
                    return Capability(True, "", ident, "busy" if status_type == "active" else "idle")
                return Capability(False, f"Codex thread status: {status_type}", ident, status_type)
        except HostError as exc:
            return Capability(False, str(exc), binding.get("thread_id") if isinstance(binding, dict) else None)

    async def deliver(self, binding: dict[str, Any], event_id: str, prompt: str) -> Delivery:
        ident = _thread_id(binding)
        if not _EVENT_ID.fullmatch(event_id):
            raise HostError("Invalid handoff event id")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HostError("Handoff prompt is empty")
        marker = f"Codex1CC event_id={event_id}"
        connection = _Connection(await self._socket_path(), self._approval_context(binding))
        await connection.__aenter__()
        keep_open = False
        try:
            result = await connection.request("thread/read", {"threadId": ident, "includeTurns": True})
            thread = result.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != ident:
                raise HostError("Registered Codex thread was not found")
            if thread.get("historyMode") != "legacy":
                raise HostError("Codex thread history cannot be resumed reliably")
            expected_cwd = binding.get("project_root") or binding.get("cwd")
            if expected_cwd and Path(thread.get("cwd", "")).resolve() != Path(expected_cwd).resolve():
                raise HostError("Codex thread belongs to a different project directory")
            existing = _find_marker(thread, marker)
            if existing:
                return _turn_result(existing)
            status = thread.get("status") or {}
            if isinstance(status, dict) and status.get("type") == "active":
                return Delivery(None, "busy")
            await connection.request("thread/resume", {"threadId": ident})
            await self._check_handoff_tools(connection, ident)
            started = await connection.request("turn/start", {
                "threadId": ident,
                "approvalPolicy": "on-request",
                "sandboxPolicy": {"type": "readOnly"},
                "input": [{"type": "text", "text": f"{marker}\n\n{prompt}"}],
            }, timeout=30)
            turn = started.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise HostError("Codex App Server started a turn without a turn id; outcome is uncertain")
            if turn.get("status") == "inProgress":
                self._live_connections[turn["id"]] = connection
                keep_open = True
            return _turn_result(turn)
        finally:
            if not keep_open:
                await connection.__aexit__(None, None, None)

    async def reconcile(self, binding: dict[str, Any], turn_id: str) -> Delivery:
        ident = _thread_id(binding)
        async with _Connection(await self._socket_path()) as connection:
            result = await connection.request("thread/read", {"threadId": ident, "includeTurns": True})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != ident:
            raise HostError("Registered Codex thread was not found")
        for turn in thread.get("turns") or []:
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return _turn_result(turn)
        return Delivery(turn_id, "unknown", error="Turn not found in persisted Codex history")

    async def interrupt_turn(self, binding: dict[str, Any], turn_id: str) -> None:
        ident = _thread_id(binding)
        if not isinstance(turn_id, str) or not turn_id:
            raise HostError("A Codex turn id is required")
        async with _Connection(await self._socket_path()) as connection:
            await connection.request("turn/interrupt", {"threadId": ident, "turnId": turn_id})

    async def reconcile_event(self, binding: dict[str, Any], event_id: str) -> Delivery:
        ident = _thread_id(binding)
        if not _EVENT_ID.fullmatch(event_id):
            raise HostError("Invalid handoff event id")
        async with _Connection(await self._socket_path()) as connection:
            result = await connection.request("thread/read", {"threadId": ident, "includeTurns": True})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != ident:
            raise HostError("Registered Codex thread was not found")
        turn = _find_marker(thread, f"Codex1CC event_id={event_id}")
        return _turn_result(turn) if turn else Delivery(None, "unknown", error="Event not found in persisted Codex history")

    async def wait_for_turn(self, binding: dict[str, Any], turn_id: str, *, timeout: float = 3600) -> Delivery:
        ident = _thread_id(binding)
        deadline = asyncio.get_running_loop().time() + timeout
        connection = self._live_connections.pop(turn_id, None)
        if connection is None:
            connection = _Connection(await self._socket_path(), self._approval_context(binding))
            await connection.__aenter__()
        try:
            await connection.request("thread/resume", {"threadId": ident})
            # Subscribe before reading history, so completion cannot fall into a gap.
            result = await connection.request("thread/read", {"threadId": ident, "includeTurns": True})
            thread = result.get("thread")
            if isinstance(thread, dict):
                for turn in thread.get("turns") or []:
                    if isinstance(turn, dict) and turn.get("id") == turn_id and turn.get("status") != "inProgress":
                        return _turn_result(turn)
            try:
                while True:
                    message = await asyncio.wait_for(connection.messages.get(), max(0, deadline - asyncio.get_running_loop().time()))
                    if message.get("method") == "transport/disconnected":
                        return Delivery(turn_id, "unknown", error="Codex App Server disconnected")
                    if message.get("method") == "turn/completed":
                        params = message.get("params") or {}
                        turn = params.get("turn") or {}
                        if params.get("threadId") == ident and turn.get("id") == turn_id:
                            return _turn_result(turn)
                    elif message.get("method") == "client/request_rejected":
                        return Delivery(turn_id, "unknown", error="Interactive request rejected during automatic handoff")
                    elif message.get("method") == "thread/status/changed":
                        status = (message.get("params") or {}).get("status") or {}
                        if status.get("type") in {"notLoaded", "systemError"}:
                            return Delivery(turn_id, "unknown", error="Codex thread stopped before completion was observed")
            except asyncio.TimeoutError:
                return Delivery(turn_id, "unknown", error="Timed out waiting for Codex completion")
        finally:
            await connection.__aexit__(None, None, None)

    async def wait_for_idle(self, binding: dict[str, Any], *, timeout: float = 3600) -> Capability:
        ident = _thread_id(binding)
        deadline = asyncio.get_running_loop().time() + timeout
        async with _Connection(await self._socket_path()) as connection:
            await connection.request("thread/resume", {"threadId": ident})
            result = await connection.request("thread/read", {"threadId": ident})
            thread = result.get("thread") or {}
            status = thread.get("status") or {}
            if status.get("type") == "idle":
                return Capability(True, "", ident, "idle")
            if status.get("type") != "active":
                return Capability(False, f"Codex thread status: {status.get('type', 'unknown')}", ident)
            try:
                while True:
                    message = await asyncio.wait_for(connection.messages.get(), max(0, deadline - asyncio.get_running_loop().time()))
                    if message.get("method") == "transport/disconnected":
                        return Capability(False, "Codex App Server disconnected", ident)
                    if message.get("method") == "thread/status/changed":
                        params = message.get("params") or {}
                        if params.get("threadId") == ident:
                            state = (params.get("status") or {}).get("type")
                            if state == "idle":
                                return Capability(True, "", ident, "idle")
                            if state in {"systemError", "notLoaded"}:
                                return Capability(False, f"Codex thread status: {state}", ident)
            except asyncio.TimeoutError:
                return Capability(False, "Timed out waiting for Codex thread to become idle", ident)


def _find_marker(thread: dict[str, Any], marker: str) -> dict[str, Any] | None:
    for turn in thread.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items") or []:
            if not isinstance(item, dict) or item.get("type") != "userMessage":
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str) and content["text"].startswith(marker + "\n"):
                    return turn
    return None
