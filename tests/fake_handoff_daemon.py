"""Fake CC and Codex host for end-to-end event delivery tests."""

import asyncio
import json
import os
from pathlib import Path
import sqlite3

from codex1cc import daemon, handoff_host


async def call(method, params):
    reader, writer = await asyncio.open_unix_connection(os.environ["CODEX1CC_STATE_DIR"] + "/daemon.sock")
    writer.write((json.dumps({"method": method, "params": params}) + "\n").encode())
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    if not response["ok"]:
        raise RuntimeError(response)
    return response["data"]


def fake_wrap(cli, command, snapshot, mcp_config):
    return command


class FakeHost:
    async def probe(self, binding):
        return handoff_host.Capability(True, thread_id=binding["thread_id"], status="idle")

    async def deliver(self, binding, event_id, prompt):
        record = Path(os.environ["CODEX1CC_STATE_DIR"]) / "delivered.jsonl"
        with record.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event_id": event_id, "thread_id": binding["thread_id"],
                                     "prompt": prompt}) + "\n")
        return handoff_host.Delivery("fake-turn-" + event_id, "inProgress")

    async def reconcile(self, binding, turn_id):
        return handoff_host.Delivery(turn_id, "inProgress")

    async def wait_for_turn(self, binding, turn_id, *, timeout=600):
        event_id = turn_id.removeprefix("fake-turn-")
        database = Path(os.environ["CODEX1CC_STATE_DIR"]) / "state.sqlite3"
        if os.environ.get("CODEX1CC_TEST_AUTONOMOUS") == "1":
            # The simulated controller drives actual daemon operations, worktrees, and question transport.
            task_id = binding["_handoff_task_id"]
            report = await call("get_task", {"task_id": task_id})
            task = report["task"]
            request_id = "decision-" + event_id
            if task["status"] == "failed":
                worktree = Path(task["workspace"]["worktree_path"])
                assert (worktree / "README.txt").read_text() == "partial"
                head = task["result"]["workspace"]["head_commit"]
                await call("continue_task", {"task_id": task_id, "fresh_session": True,
                    "scope": ["README.txt", "FIX.txt"], "expected_head": head,
                    "review_note": "Inspected partial README and commit; FIX is inside project authorization",
                    "instruction": "Preserve partial work, write FIX.txt, ask bridge question and finish README",
                    "request_id": request_id})
                outcome = "continued"
            elif task["status"] == "waiting_answer":
                await call("respond_task", {"task_id": task_id, "question_id": report["questions"][0]["id"],
                    "answer": "verified", "request_id": request_id})
                outcome = "answered"
            elif task["status"] == "review_required":
                worktree = Path(task["workspace"]["worktree_path"])
                assert (worktree / "README.txt").read_text() == "fixed"
                assert (worktree / "FIX.txt").read_text() == "verified"
                await call("complete_task", {"task_id": task_id,
                    "review_note": "Verified repaired files and original acceptance", "request_id": request_id,
                    "acceptance_evidence": ["README=fixed and FIX=verified"],
                    "expected_head": task["result"]["workspace"]["head_commit"]})
                outcome = "completed"
            else:
                raise RuntimeError("Unexpected controller state: " + task["status"])
            await call("ack_handoff", {"event_id": event_id, "receipt_token": binding["_handoff_receipt_token"],
                                       "outcome": outcome})
            return handoff_host.Delivery(turn_id, "completed", "Controller " + outcome)
        for _ in range(100):
            with sqlite3.connect(database) as connection:
                row = connection.execute("SELECT status FROM handoff_events WHERE id=?", (event_id,)).fetchone()
            if row and row[0] == "handled":
                return handoff_host.Delivery(turn_id, "completed", "Reviewed task")
            await asyncio.sleep(0.02)
        return handoff_host.Delivery(turn_id, "unknown", error="Fake host timed out")


handoff_host.AppServerHost = FakeHost
daemon.wrap_linux = fake_wrap
asyncio.run(daemon.serve())
