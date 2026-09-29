"""Fake CC and Codex host for end-to-end event delivery tests."""

import asyncio
import json
import os
from pathlib import Path
import sqlite3

from codex1cc import daemon, handoff_host


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
