"""Persisted goal recovery and transport failure behavior."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex1cc.daemon import Executor
from codex1cc.store import Store


class GoalRecoveryTest(unittest.TestCase):
    def test_restart_recovers_incomplete_goal_once_and_keeps_original_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = Store(path)
            bundle = {"objective": "Finish original objective", "acceptance": ["All gates pass"],
                      "autonomous": True, "backend": "native_write",
                      "handoff": {"mode": "automatic", "thread_id": "thread"}}
            store.create("task", "project", "request", bundle,
                         {"write_backend": {"write_paths": ["src", "tests"]}}, "unused")
            store.update("task", status="failed")
            executor = Executor.__new__(Executor)
            executor.store = store
            executor.recover_goals()
            executor.recover_goals()
            self.assertEqual(store.handoff_status("task")["counts"], {"pending": 1})
            store.db.close()
            executor.store = Store(path)
            try:
                executor.recover_goals()
                task = executor.store.one("task")
                self.assertEqual(task["goal"]["status"], "active")
                self.assertEqual(task["goal"]["acceptance"], ["All gates pass"])
                self.assertEqual(task["goal"]["authorized_paths"], ["src", "tests"])
                self.assertEqual(executor.store.handoff_status("task")["counts"], {"pending": 1})
                self.assertTrue(Executor._handoff_current(executor.store.handoff_ready()[0], task))
                event_id = executor.store.handoff_ready()[0]["id"]
                executor.store.handoff_flag(event_id, "Unknown effects")
                executor.recover_goals()
                self.assertEqual(executor.store.goal("task")["status"], "blocked")
                self.assertEqual(executor.store.handoff_status("task")["counts"], {"needs_reconcile": 1})
                notifications = executor.store.db.execute("SELECT message FROM notifications").fetchall()
                self.assertIn("uncertain", notifications[-1]["message"])
            finally:
                executor.store.db.close()


class GoalWaitTest(unittest.IsolatedAsyncioTestCase):
    async def test_progress_wakeups_do_not_reset_the_wait_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            bundle = {"objective": "Finish", "acceptance": ["Pass"], "autonomous": True,
                      "backend": "native_write", "handoff": {"mode": "automatic", "thread_id": "thread"}}
            store.create("task", "project", "request", bundle,
                         {"write_backend": {"write_paths": ["src"]}}, "unused")
            executor = Executor.__new__(Executor)
            executor.store = store
            executor.activity_changed = mock.Mock()
            executor.stopping = False
            executor.activity_changed.wait = mock.AsyncMock(return_value=True)
            clock = mock.Mock()
            clock.monotonic.side_effect = iter(range(50))
            try:
                with mock.patch("codex1cc.daemon.time", clock):
                    result = await executor.dispatch("wait_task", {"task_id": "task", "cursor": 2**63 - 1})
                self.assertEqual(result["task"]["goal"]["status"], "active")
                self.assertEqual(result["events"], [])
                self.assertGreaterEqual(clock.monotonic.call_count, 30)
            finally:
                store.db.close()
