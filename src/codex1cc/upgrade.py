"""Drain existing workers before activating the installed executor."""

from __future__ import annotations

import asyncio
import json
import sqlite3

from .common import SOCKET, STATE, BridgeError, rpc
from .handoff_host import AppServerHost, HostError, _Connection
from .skill_install import install_skill


async def upgrade_executor(adopt_task: str | None = None) -> None:
    timed_out: set[str] = set()
    while True:
        report = await rpc("list_tasks", {"statuses": ["queued", "running", "continuing", "waiting_answer",
            "review_required", "failed", "interrupted", "completed", "canceled"], "limit": 100})
        busy = [task for task in report["tasks"] if task["status"] in {"queued", "running", "continuing", "waiting_answer"}
                or any(task.get("handoff", {}).get("counts", {}).get(s) for s in ("sending", "accepted"))
                or (task.get("handoff", {}).get("latest") or {}).get("codex_status") == "inProgress"]
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
            await rpc("shutdown", autostart=False)
        except BridgeError as exc:
            if exc.code == "INVALID_STATE":
                continue  # A controller started a new worker while draining.
            if exc.code != "DAEMON_UNAVAILABLE":
                raise
        break
    for _ in range(100):
        if not SOCKET.exists():
            break
        await asyncio.sleep(0.1)
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
        host_reload = str(exc)
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


def _event_cursor(task_id: str) -> int:
    """Wait for the next real event without replaying a large legacy task history."""
    try:
        with sqlite3.connect(f"file:{STATE / 'state.sqlite3'}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT MAX(seq) FROM events WHERE task_id=?", (task_id,)).fetchone()
            return row[0] or 0
    except sqlite3.Error:
        return 0
