"""Failure injection for the worker/controller lifecycle, without model calls."""

import asyncio
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

from codex1cc.common import BridgeError
from codex1cc.daemon import Executor
from codex1cc.handoff_host import Delivery
from codex1cc.store import Store


class LifecycleAuditTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        with mock.patch("codex1cc.daemon.Store", return_value=self.store):
            self.executor = Executor(asyncio.Event())
        self.bundle = {"objective": "finish", "acceptance": ["verified"],
                       "backend": "read_only", "scope": ["README.txt"],
                       "handoff": {"mode": "automatic", "thread_id": "thread"}}
        self.store.create("task", "project", "request", self.bundle, {}, "unused")
        self.store.update("task", status="running", round_no=1)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def test_failure_keeps_worker_owned_until_cleanup_finishes(self):
        job = asyncio.get_running_loop().create_future()
        self.executor.jobs["task"] = job
        self.executor._fail("task", "CLI_FAILED", "injected")
        self.assertIs(self.executor.jobs.get("task"), job)

    async def test_failure_expires_question_and_unblocks_bridge(self):
        question = asyncio.create_task(self.executor.ask({"task_id": "task", "text": "choice?"}))
        await asyncio.sleep(0)
        self.assertEqual(len(self.store.pending_questions("task")), 1)
        self.executor._fail("task", "CLI_FAILED", "worker exited during question")
        try:
            with self.assertRaises(BridgeError) as error:
                await asyncio.wait_for(question, 0.2)
            self.assertEqual(error.exception.code, "QUESTION_EXPIRED")
            self.assertEqual(self.store.pending_questions("task"), [])
            self.assertEqual(self.store.one("task")["status"], "failed")
        finally:
            question.cancel()
            await asyncio.gather(question, return_exceptions=True)

    def delivered(self, handled=False):
        self.store.transition("task", "review_required", {}, status="review_required")
        event = self.store.handoff_ready()[0]
        self.store.handoff_accept(event["id"], "turn")
        if handled:
            self.store.handoff_handle(event["id"], "turn", "reviewed")
        return event

    async def test_shutdown_refuses_controller_still_running_after_receipt(self):
        self.delivered(handled=True)
        with self.assertRaises(BridgeError) as error:
            await self.executor.dispatch("shutdown", {})
        self.assertEqual(error.exception.code, "INVALID_STATE")

    async def test_handled_controller_still_reserves_its_thread(self):
        self.delivered(handled=True)
        self.assertIn("thread", self.store.handoff_busy_threads())

    async def test_restart_resubscribes_to_handled_but_running_turn(self):
        event = self.delivered(handled=True)
        host = mock.Mock()
        host.reconcile = mock.AsyncMock(return_value=Delivery("turn", "inProgress"))
        observed = asyncio.Event()

        async def observe(*args, **kwargs):
            observed.set()
            return Delivery("turn", "completed", "verified")

        host.wait_for_turn = mock.AsyncMock(side_effect=observe)
        with mock.patch("codex1cc.handoff_host.AppServerHost", return_value=host):
            loop = asyncio.create_task(self.executor.handoff_loop())
            try:
                await asyncio.wait_for(observed.wait(), 0.3)
                await asyncio.sleep(0)
                self.assertEqual(self.store.handoff_one(event["id"])["codex_status"], "completed")
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_acknowledged_turn_observer_failure_becomes_visible(self):
        event = self.delivered(handled=True)
        host = mock.Mock()
        host.reconcile = mock.AsyncMock(side_effect=OSError("injected disconnect"))
        with mock.patch("codex1cc.handoff_host.AppServerHost", return_value=host):
            loop = asyncio.create_task(self.executor.handoff_loop())
            try:
                await asyncio.sleep(0.03)
                stored = self.store.handoff_one(event["id"])
                self.assertEqual(stored["status"], "needs_reconcile")
                self.assertIn("disconnect", stored["error"])
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_stale_round_event_cannot_trigger_a_new_review(self):
        event = self.delivered()
        self.store.update("task", round_no=2)
        self.assertFalse(self.executor._handoff_current(event, self.store.one("task")))

    async def test_pending_goal_reserves_project_between_rounds(self):
        self.store.create_goal("task", {**self.bundle, "authorized_scope": ["README.txt"]}, {})
        self.store.update("task", status="failed")
        with self.assertRaises(BridgeError) as error:
            self.executor._admit_parallel("project", {}, {"scope": ["README.txt"], "parallel_ok": False})
        self.assertEqual(error.exception.code, "PROJECT_BUSY")

    async def test_revoked_write_permission_applies_to_legacy_continuation(self):
        task = self.store.one("task")
        task["bundle"]["backend"] = "native_write"
        task["project"]["root"] = "/project"
        with mock.patch("codex1cc.daemon.project_config", return_value={
                "root": "/project", "write_backend": {"enabled": False}}):
            with self.assertRaises(BridgeError) as error:
                self.executor._current_project(task)
        self.assertEqual(error.exception.code, "PROJECT_NOT_ALLOWED")

    async def test_pre_spawn_exception_records_failure_and_releases_job(self):
        with mock.patch.object(self.executor, "_run_task", mock.AsyncMock(side_effect=OSError("disk full"))):
            job = asyncio.create_task(self.executor.run_task("task"))
            self.executor.jobs["task"] = job
            await job
        self.assertEqual(self.store.one("task")["status"], "failed")
        self.assertNotIn("task", self.executor.jobs)

    async def test_cancel_unblocks_pending_question_without_resurrecting_task(self):
        question = asyncio.create_task(self.executor.ask({"task_id": "task", "text": "choice?"}))
        await asyncio.sleep(0)
        await self.executor.cancel({"task_id": "task", "request_id": "cancel"})
        with self.assertRaises(BridgeError):
            await asyncio.wait_for(question, 0.2)
        self.assertEqual(self.store.one("task")["status"], "canceled")
        self.assertFalse(self.store.pending_questions("task"))

    async def test_observer_disconnect_after_receipt_requires_reconciliation(self):
        event = self.delivered(handled=True)
        host = mock.Mock()
        host.reconcile = mock.AsyncMock(return_value=Delivery("turn", "inProgress"))
        host.wait_for_turn = mock.AsyncMock(return_value=Delivery("turn", "unknown", error="disconnected"))
        with mock.patch("codex1cc.handoff_host.AppServerHost", return_value=host):
            loop = asyncio.create_task(self.executor.handoff_loop())
            try:
                await asyncio.sleep(0.03)
                self.assertEqual(self.store.handoff_one(event["id"])["status"], "needs_reconcile")
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)

    async def test_queued_task_rechecks_permission_before_spawning(self):
        with mock.patch.object(self.executor, "_current_project", side_effect=BridgeError(
                "PROJECT_NOT_ALLOWED", "write authorization revoked")), \
             mock.patch("codex1cc.daemon.asyncio.create_subprocess_exec", mock.AsyncMock()) as spawn:
            await self.executor.run_task("task")
        spawn.assert_not_awaited()
        self.assertEqual(self.store.one("task")["exit_reason"], "PROJECT_NOT_ALLOWED")

    async def test_service_failure_is_visible_then_supervision_recovers(self):
        calls = 0
        observed = []

        async def service():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected transport failure")
            self.executor.stopping = True

        async def delay(seconds):
            observed.append(self.executor.service_health["handoff"].copy())

        with mock.patch("codex1cc.daemon.asyncio.sleep", side_effect=delay):
            await self.executor.supervise("handoff", service)
        self.assertEqual(calls, 2)
        self.assertEqual(observed[0]["status"], "retrying")
        self.assertIn("injected", observed[0]["error"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process ownership recovery")
    async def test_crash_recovery_stops_verified_writer_before_making_task_resumable(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)", "task"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.store.update("task", process_id=process.pid)
            self.store.mark_interrupted()
            process.wait(timeout=3)
            self.assertLess(process.returncode, 0)
            task = self.store.one("task")
            self.assertEqual(task["status"], "interrupted")
            self.assertEqual(task["exit_reason"], "daemon_restart")
            self.assertFalse(self.store._group_alive(process.pid))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
