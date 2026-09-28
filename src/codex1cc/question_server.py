"""Claude-facing MCP tool for an actual blocking question."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .common import rpc

server = FastMCP("Codex1CC Questions")


@server.tool()
async def ask_codex(text: str, options: list[str] | None = None) -> str:
    """Ask one blocking question. Combine related choices into this question."""
    task_id = os.environ["CODEX1CC_TASK_ID"]
    result = await rpc("ask_question", {"task_id": task_id, "text": text,
                                        "options": options or []},
                       autostart=False, timeout=24 * 3600 + 30)
    return result["answer"]


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
