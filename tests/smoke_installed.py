"""Run from a clean wheel installation; never uses project models or state."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    with tempfile.TemporaryDirectory(prefix="codex1cc-install-") as directory:
        root = Path(directory)
        config = root / "projects.json"
        config.write_text('{"projects": {}}')
        config.chmod(0o600)
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env.update(CODEX1CC_STATE_DIR=str(root / "state"), CODEX1CC_CONFIG=str(config),
                   CODEX1CC_DESKTOP_NOTIFICATIONS="0")
        command = [sys.executable, "-I", "-m", "codex1cc.cli"]
        try:
            result = subprocess.run([*command, "doctor"], env=env, capture_output=True,
                                    text=True, check=True, timeout=30)
            report = json.loads(result.stdout)
            assert report["protocol_version"] == 3
            assert report["instance_id"]
            assert all(s["status"] == "running" for s in report["services"].values())
            params = StdioServerParameters(command=sys.executable,
                args=["-I", "-m", "codex1cc.mcp_server"], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    assert {"submit_task", "continue_task", "manage_goal", "complete_task", "ack_handoff"} <= tools.keys()
                    assert "authorized_scope" in tools["submit_task"].inputSchema["properties"]
                    result = await session.call_tool("list_tasks", {"statuses": ["running"]})
                    assert not result.isError
            print("Installed wheel: daemon, health, MCP handshake, schemas and RPC passed; no model calls")
        finally:
            subprocess.run([*command, "stop"], env=env, capture_output=True, timeout=15, check=False)
            # shutdown acknowledges before the server removes its endpoint.
            for _ in range(100):
                if not (root / "state" / "daemon.sock").exists():
                    break
                await asyncio.sleep(0.05)
            assert not (root / "state" / "daemon.sock").exists(), "Executor did not exit"


if __name__ == "__main__":
    asyncio.run(main())
