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

Read-only tasks use `actions=["read"]`. For a coding or test task, first check
that the project explicitly enables `write_backend` and that the user's stated
scope is within its `write_paths`. Explain once that this mode lets CC run
commands in a trusted project and does not enforce a filesystem or network
sandbox. Use `actions=["read","write","execute"]`; do not submit it as a
read-only task. A task worktree starts from the project's committed HEAD, so
check that this HEAD contains any prior branch or worktree the task depends on;
if it does not, report the baseline mismatch before submission. Report when
the source workspace has uncommitted changes. Never enable the
write backend in project configuration merely because a task asked to edit;
the user must explicitly authorize that project-level trust choice.

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
duplicate. Build one cohesive task with the complete objective, relevant public
context, measurable acceptance criteria, expected deliverables, exact authorized
scope, time and round limits, and question policy. Do not set or negotiate a CC
USD limit; cost is optional usage telemetry, not an approval gate. For Linux
write tasks, `limits.seconds` counts CC agent work and pauses while an external
test/build/gate descendant is running; `limits.wall_seconds` is the whole-round
hard cap. Do not describe either limit as a cost budget. Keep linked small steps
together. Split work only when tasks are truly independent. The default project limit is 3; for
parallel work in one project, require an effective `parallel.max_agents > 1`
and explicit authorization that the tasks are
independent. Inspect each active task's scope with `get_task` as needed. Set
`parallel_ok=true` only when the tasks have no semantic dependency or path
overlap; each accepted task gets its own CC process, session, and write
worktree. If a task conflicts or reaches the agent limit, keep it serial:
report the blocker and submit it only after the earlier task is reviewed and
its changes are integrated into the new baseline. Do not schedule MCP polling
or silently resubmit the rejected task.

Call `submit_task` once with a new stable `request_id`. Reuse that same ID only
to retry an uncertain identical request. Set `handoff="automatic"` when the
user requested automatic takeover. Return the task ID and reported handoff
status, then stop; do not schedule `list_tasks` or `get_task` polling. For a
write task, include its task branch and worktree, and note whether source
uncommitted changes were excluded.

## Handle delivered events

When Codex1CC delivers a question, result, failure, or interruption:

1. Call `get_task` for that task and verify the current state, original
   acceptance criteria, evidence, and pending question before acting.
2. Answer when the existing evidence and acceptance criteria determine the
   answer. Ask the user for a genuine product choice or new project permission.
   If the current task scope is too narrow but the original objective and
   project write authorization already cover the needed files, review the
   partial result and submit a new scoped follow-up from a verified Git baseline.
   Do not ask for approval solely to raise a CC cost limit or change a task scope.
3. For write tasks, inspect the actual branch diff, commits, dirty files,
   outside-scope report, and test evidence. Treat CC's claims as unverified.
   Review evidence before `complete_task`. Use `continue_task` only for a
   concrete gap inside the original authorization. Never automatically retry a
   failed or interrupted task. A failed write task may contain valid work. After
   checking the diff, commits, scope, and test evidence, use
   `continue_task(..., fresh_session=true)` to let a new CC session finish the
   retained work in the same worktree. Give it an exact instruction naming the
   verified work, remaining gaps, and required tests. Do not resume the failed
   CC session. If the retained result already satisfies acceptance,
   `complete_task` may explicitly accept it while retaining the original failure
   reason.
   Keep the worktree until the user explicitly
   requests cleanup; do not merge, push, or deploy on task completion alone.
   When a reviewed write task has a documented next phase, use
   `continue_task(..., fresh_session=true)` if a new context is
   needed. Check the existing commits, dirty files, and test evidence first;
   this starts a new CC session in the same managed worktree and counts as a
   new round.
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

On `CONTEXT_LIMIT`, inspect the retained task worktree, commits, changed files,
and test logs. Do not resume or retry the overflowing Claude session. After an
explicit review, a failed write task can use a fresh-session relay in its
retained worktree. Do not silently restart it. Read-only failures still require
a new scoped task on a verified baseline. Do not claim to manage Claude
background jobs that were started outside Codex1CC.

Do not expose receipt tokens, configuration contents, credentials, private
absolute paths, or raw internal event logs in the user-facing response.
