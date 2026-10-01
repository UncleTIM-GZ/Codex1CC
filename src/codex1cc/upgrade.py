"""Drain existing workers before activating the installed executor."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

from .common import SOCKET, STATE, BridgeError, rpc
from .handoff_host import AppServerHost, HostError, _Connection
from .skill_install import install_skill


async def upgrade_executor(adopt_task: str | None = None) -> None:
    timed_out: set[str] = set()
    old_instance = None
    while True:
        tasks = []
        offset = 0
        while True:
            report = await rpc("list_tasks", {"statuses": ["queued", "running", "continuing", "waiting_answer",
                "review_required", "failed", "interrupted", "completed", "canceled"], "limit": 100, "offset": offset})
            tasks.extend(report["tasks"])
            if len(report["tasks"]) < 100:
                break
            offset += 100
        busy = [task for task in tasks if task["status"] in {"queued", "running", "continuing", "waiting_answer"}
                or any(task.get("handoff", {}).get("counts", {}).get(s) for s in ("sending", "accepted"))
                or _controller_active(task) or _worker_still_alive(task["id"])]
        if busy:
            task = busy[0]
            print(json.dumps({"upgrade": "waiting_for_idle", "task_id": task["id"],
                              "status": task["status"]}), flush=True)
            if task["id"] in timed_out:
                await asyncio.sleep(15)
            else:
                try:
                    await rpc("wait_task", {"task_id": task["id"], "cursor": _event_cursor(task["id"])}, timeout=40)
                except (asyncio.TimeoutError, TimeoutError):
                    # Do not accumulate disconnected legacy waiters on continuous progress.
                    timed_out.add(task["id"])
            # Older wait_task can return immediately once acceptance is recorded.
            await asyncio.sleep(1)
            continue
        try:
            stopped = await rpc("shutdown", autostart=False)
            old_instance = stopped.get("instance_id")
        except BridgeError as exc:
            if exc.code == "INVALID_STATE":
                await asyncio.sleep(1)
                continue  # A controller started a new worker while draining.
            if exc.code != "DAEMON_UNAVAILABLE":
                raise
        break
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not SOCKET.exists():
            break
        try:
            running = await rpc("doctor", autostart=False)
            if ((old_instance and running.get("instance_id") not in {None, old_instance}) or
                    (not old_instance and running.get("instance_id"))):
                break  # Another MCP client already started the replacement executor.
        except (BridgeError, OSError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(0.25)
    else:
        raise BridgeError("DAEMON_UNAVAILABLE", "Old executor did not release its socket")
    backend = await rpc("doctor")
    if backend.get("protocol_version", 0) < 3:
        raise BridgeError("DAEMON_UNAVAILABLE", "Installed executor is older than protocol 3")
    install_skill(force=True)
    try:
        host = AppServerHost()
        async with _Connection(await host._socket_path()) as connection:
            await connection.request("config/mcpServer/reload", {})
        host_reload = "queued"
    except HostError as exc:
        raise BridgeError("HANDOFF_UNAVAILABLE", "Executor restarted, but MCP refresh failed: " + str(exc)) from exc
    print(json.dumps({"upgrade": "activated", "protocol_version": backend["protocol_version"],
                      "mcp_reload": host_reload}), flush=True)
    if adopt_task:
        task = (await rpc("get_task", {"task_id": adopt_task}))["task"]
        if task["status"] not in {"completed", "canceled"}:
            await rpc("manage_goal", {"task_id": adopt_task, "status": "active",
                "reason": "User requested autonomous control upgrade; preserve existing work and complete original acceptance",
                "request_id": "upgrade-adopt-" + adopt_task})
            print(json.dumps({"upgrade": "goal_adopted", "task_id": adopt_task}), flush=True)
        await rpc("announce_upgrade", {"task_id": adopt_task})


def _controller_active(task: dict) -> bool:
    latest = task.get("handoff", {}).get("latest") or {}
    return latest.get("codex_status") == "inProgress" or bool(
        latest.get("status") == "handled" and latest.get("turn_id") and latest.get("codex_status") is None)


def _worker_still_alive(task_id: str) -> bool:
    """Old executors can label a live worker failed; drain the saved PID too."""
    try:
        with sqlite3.connect(f"file:{STATE / 'state.sqlite3'}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT process_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    except sqlite3.Error as exc:
        raise BridgeError("INVALID_STATE", "Cannot verify saved workers before upgrading") from exc
    if not row or not row[0]:
        return False
    try:
        if Path("/proc").is_dir():
            state = Path(f"/proc/{row[0]}/stat").read_text().rpartition(")")[2].split()[0]
            return state != "Z"
        os.kill(row[0], 0)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _event_cursor(task_id: str) -> int:
    """Wait for the next real event without replaying a large legacy task history."""
    try:
        with sqlite3.connect(f"file:{STATE / 'state.sqlite3'}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT MAX(seq) FROM events WHERE task_id=?", (task_id,)).fetchone()
            return row[0] or 0
    except sqlite3.Error:
        return 0
