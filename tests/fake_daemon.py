"""Run the real executor with only its sandbox wrapper replaced by a fake."""

import asyncio

from codex1cc import daemon


def fake_wrap(cli, command, snapshot, mcp_config):
    return command


daemon.wrap_linux = fake_wrap

asyncio.run(daemon.serve())
