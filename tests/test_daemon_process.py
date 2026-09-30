"""Focused process lifecycle tests for the durable executor."""

from __future__ import annotations

import asyncio
import signal
import unittest
from unittest import mock

from codex1cc.daemon import Executor


class _ExitingProcess:
    pid = 12345
    returncode = None

    async def wait(self) -> int:
        raise asyncio.TimeoutError


class StopProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_process_exit_during_kill_escalation_is_already_stopped(self) -> None:
        executor = Executor.__new__(Executor)
        executor.processes = {"task": _ExitingProcess()}

        with mock.patch("codex1cc.daemon.os.killpg") as killpg:
            killpg.side_effect = [None, ProcessLookupError]
            await executor._stop_process("task")

        self.assertEqual(
            killpg.call_args_list,
            [mock.call(12345, signal.SIGTERM), mock.call(12345, signal.SIGKILL)],
        )
