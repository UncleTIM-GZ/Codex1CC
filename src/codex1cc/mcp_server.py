"""Codex-facing stdio MCP tools."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .common import BridgeError, rpc

server = FastMCP("Codex1CC", instructions=(
    "Delegate one complete cohesive task. Do not poll. Automatic handoff requires a configured "
    "Codex host binding and submit_task(handoff='automatic'). When handling a delivered event, "
    "review the task and call ack_handoff after taking action. Review artifacts before complete_task."
    " Automatic write tasks keep an active goal until all original acceptance passes. After a worker failure,"
    " review evidence and continue in the retained worktree, adjusting scope within original authorization."
    " A next-step summary does not complete the goal. Use manage_goal only for concrete blockers or user decisions."
))


@server.tool()
async def submit_task(project_id: str, objective: str, context: str,
                      acceptance: list[str], deliverables: list[str], scope: list[str],
                      request_id: str, question_policy: str = "",
                      limits: dict | None = None, actions: list[str] | None = None,
                      handoff: str | None = None, parallel_ok: bool = False,
                      autonomous: bool | None = None, authorized_scope: list[str] | None = None) -> dict:
    """Submit a task. Automatic write tasks keep an autonomous goal.

    scope limits this CC round. Set authorized_scope if the user restricts paths for the entire goal;
    otherwise repairs may use initial project write authorization while preserving the original objective.
    """
    if handoff in {None, "automatic"}:
        backend = await rpc("doctor")
        required_version = 3 if handoff is None or actions == ["read", "write", "execute"] and autonomous is not False else 2
        if backend.get("protocol_version", 0) < required_version:
            raise BridgeError("HANDOFF_UNAVAILABLE", "Codex1CC executor must be upgraded and restarted")
    return await rpc("submit_task", {"project_id": project_id, "objective": objective,
                                     "context": context, "acceptance": acceptance,
                                     "deliverables": deliverables, "scope": scope,
                                     "request_id": request_id, "question_policy": question_policy,
                                     "limits": limits or {}, "actions": actions if actions is not None else ["read"],
                                     "handoff": handoff, "parallel_ok": parallel_ok, "autonomous": autonomous,
                                     "authorized_scope": authorized_scope})


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
async def continue_task(task_id: str, instruction: str, request_id: str,
                        fresh_session: bool = False, scope: list[str] | None = None,
                        review_note: str = "", expected_head: str = "") -> dict:
    """Continue verified work in the retained worktree. Goals require review_note and inspected expected_head.

    For authorized repairs, pass a revised scope and fresh_session=True. Original acceptance stays fixed.
    """
    return await rpc("continue_task", {"task_id": task_id, "instruction": instruction,
                                       "request_id": request_id,
                                       "fresh_session": fresh_session, "scope": scope,
                                       "review_note": review_note, "expected_head": expected_head})


@server.tool()
async def manage_goal(task_id: str, status: str, reason: str, request_id: str) -> dict:
    """Adopt/resume an automatic write task with active, or record blocked/needs_user with a concrete reason.

    A worker failure does not end an active goal. Continue repairs within original project authorization;
    block only on an actual technical impediment, exhausted limits, or new user decision/authorization.
    """
    return await rpc("manage_goal", {"task_id": task_id, "status": status,
                                      "reason": reason, "request_id": request_id})


@server.tool()
async def complete_task(task_id: str, review_note: str, request_id: str,
                        acceptance_evidence: list[str] | None = None, expected_head: str = "") -> dict:
    """Record acceptance. Goals require one verified evidence entry per original criterion and inspected HEAD."""
    return await rpc("complete_task", {"task_id": task_id, "review_note": review_note,
                                       "request_id": request_id, "acceptance_evidence": acceptance_evidence,
                                       "expected_head": expected_head})


@server.tool()
async def cancel_task(task_id: str, request_id: str) -> dict:
    """Cancel only this executor-owned task; retain files and history."""
    return await rpc("cancel_task", {"task_id": task_id, "request_id": request_id})


@server.tool()
async def ack_handoff(event_id: str, receipt_token: str, outcome: str) -> dict:
    """Confirm that a delivered event was handled in this Codex turn."""
    return await rpc("ack_handoff", {"event_id": event_id,
                                     "receipt_token": receipt_token, "outcome": outcome})


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
