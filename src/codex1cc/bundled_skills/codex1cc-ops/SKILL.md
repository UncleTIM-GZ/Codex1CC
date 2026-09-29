---
name: codex1cc-ops
description: Manage Codex1CC project bindings and automatic Claude Code handoffs. Use when the user asks to bind, rebind, unbind, diagnose, submit, watch, answer, continue, review, or recover a task through Codex1CC. Do not use for generic Claude Code work that is not delegated through Codex1CC.
---

# Codex1CC Operations

Operate Codex1CC for the user instead of merely restating commands. Match the
user's language. Treat an explicit request to bind, rebind, unbind, submit,
answer, continue, complete, or cancel as authorization for that operation. If
the user asks only how something works, explain it without changing state.

## Resolve the project

Use an explicit `project_id` when the user provides one. Otherwise, match the
current working directory to exactly one configured project root. Inspect only
the project keys and roots needed for that match; never print the full config,
provider settings, credentials, or model identifiers. Ask for the project ID
only when the match is absent or ambiguous.

Before a lifecycle operation, confirm that `codex1cc` is installed. Use
`codex1cc doctor` for executor and binding health. Use `codex mcp get codex1cc`
only when the MCP registration itself is in doubt.

## Bind automatic handoff

Prefer a dedicated managed Codex thread:

1. Tell the user that initialization makes one real Codex model call and may
   incur a charge. Do not pause for another confirmation when the user already
   requested the binding.
2. Run `codex app-server daemon start`.
3. Run `codex1cc bind PROJECT_ID --create`.
4. Run `codex1cc doctor` and require the project handoff to report
   `connected: true`. Return the project ID and created thread ID.

When the user supplies an existing thread ID, run
`codex1cc bind PROJECT_ID THREAD_ID`. This does not initialize a model turn, but
the thread must be resumable, use the project root, and expose the Codex1CC MCP
tools. Do not silently fall back to a new thread if validation fails.

## Update or remove a binding

Before rebinding, call `list_tasks` once for the project with statuses
`queued`, `running`, `continuing`, `waiting_answer`, and `review_required`.
Read only returned tasks whose handoff state needs clarification. If a CC task,
question, or Codex handoff is active, report the exact blocker and leave the
binding unchanged.

When clear, run `codex1cc unbind PROJECT_ID`, then bind the requested existing
thread or create a new dedicated thread. Verify with `codex1cc doctor`. If the
new bind fails, report that the project is currently unbound and preserve any
recovery thread ID printed by the command.

For an explicit unbind request, run `codex1cc unbind PROJECT_ID` and report the
number of disabled pending events. State that a Codex turn already started may
still finish. Do not cancel CC tasks unless the user separately requests it.

## Submit one task

For automatic handoff, require the project to be connected in
`codex1cc doctor`; do not downgrade silently to manual mode.

Call `list_tasks` once for active statuses before submission. Do not submit a
duplicate when the project already has an active task. Build one cohesive task
with the complete objective, relevant public context, measurable acceptance
criteria, expected deliverables, exact authorized scope, limits, and question
policy. Keep linked small steps together. Split work only when tasks are truly
independent and the configured projects allow parallel execution.

Call `submit_task` once with a new stable `request_id`. Reuse that same ID only
to retry an uncertain identical request. Set `handoff="automatic"` when the
user requested automatic takeover. Return the task ID and reported handoff
status, then stop; do not schedule `list_tasks` or `get_task` polling.

## Handle delivered events

When Codex1CC delivers a question, result, failure, or interruption:

1. Call `get_task` for that task and verify the current state, original
   acceptance criteria, evidence, and pending question before acting.
2. Answer only when the existing evidence determines the answer. Ask the user
   when the event needs a product choice, new permission, broader scope, or
   additional budget.
3. Review evidence before `complete_task`. Use `continue_task` only for a
   concrete gap inside the original authorization. Never automatically retry a
   failed or interrupted task.
4. Call `ack_handoff` with the delivered `event_id`, `receipt_token`, and the
   outcome that matches the action: `completed`, `answered`, `continued`,
   `needs_user`, `reviewed`, or `failed`.
5. Give the user a concise conclusion and next step.

## Status and recovery

For a user-requested one-time status check, call `list_tasks` once and
`get_task` only for tasks needing detail. To follow raw program events without
model polling, offer or run `codex1cc watch TASK_ID` when the user explicitly
asks to wait. Do not use repeated timed MCP calls.

On `HANDOFF_UNAVAILABLE`, run `codex1cc doctor`, report the failed layer, and
leave the task unsubmitted. On `needs_reconcile`, inspect the saved `turn_id`
and Codex result. Never redeliver the old event. Call `ack_handoff` only after
the original turn and its effects are verified; otherwise leave it for user
review.

Do not expose receipt tokens, configuration contents, credentials, private
absolute paths, or raw internal event logs in the user-facing response.
