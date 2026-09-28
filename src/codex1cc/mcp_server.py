"""Codex-facing stdio MCP tools."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .common import rpc

server = FastMCP("Codex1CC", instructions=(
    "Delegate one complete cohesive task. Do not poll. On the next natural interaction, "
    "call list_tasks to find questions or reviewable results. Review artifacts before complete_task."
))


@server.tool()
async def submit_task(project_id: str, objective: str, context: str,
                      acceptance: list[str], deliverables: list[str], scope: list[str],
                      request_id: str, question_policy: str = "",
                      limits: dict | None = None, actions: list[str] | None = None) -> dict:
    """Submit one complete, bounded task to the configured project."""
    return await rpc("submit_task", {"project_id": project_id, "objective": objective,
                                     "context": context, "acceptance": acceptance,
                                     "deliverables": deliverables, "scope": scope,
                                     "request_id": request_id, "question_policy": question_policy,
                                     "limits": limits or {}, "actions": actions or ["read"]})


@server.tool()
async def list_tasks(project_id: str | None = None, statuses: list[str] | None = None,
                     offset: int = 0, limit: int = 20) -> dict:
    """Find tasks needing an answer or review; call on demand, never on a timer."""
    return await rpc("list_tasks", {"project_id": project_id, "statuses": statuses,
                                    "offset": offset, "limit": limit})


@server.tool()
async def get_task(task_id: str, cursor: int = 0, include_events: bool = False) -> dict:
    """Read a concise task result, optionally with paginated detailed events."""
    return await rpc("get_task", {"task_id": task_id, "cursor": cursor,
                                  "include_events": include_events})


@server.tool()
async def respond_task(task_id: str, question_id: str, answer: str, request_id: str) -> dict:
    """Answer a current blocking question within existing authorization."""
    return await rpc("respond_task", {"task_id": task_id, "question_id": question_id,
                                      "answer": answer, "request_id": request_id})


@server.tool()
async def continue_task(task_id: str, instruction: str, request_id: str) -> dict:
    """Continue a reviewed Claude session with an explicit instruction."""
    return await rpc("continue_task", {"task_id": task_id, "instruction": instruction,
                                       "request_id": request_id})


@server.tool()
async def complete_task(task_id: str, review_note: str, request_id: str) -> dict:
    """Record Codex's acceptance review and mark the task complete."""
    return await rpc("complete_task", {"task_id": task_id, "review_note": review_note,
                                       "request_id": request_id})


@server.tool()
async def cancel_task(task_id: str, request_id: str) -> dict:
    """Cancel only this executor-owned task; retain files and history."""
    return await rpc("cancel_task", {"task_id": task_id, "request_id": request_id})


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
