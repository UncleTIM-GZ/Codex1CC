# Validation record

Date: 2026-09-29 (Asia/Shanghai)

## Local environment

- Linux under WSL, Python 3.10, Claude Code CLI 2.1.283, MCP Python package 1.26.0, bubblewrap 0.6.1.
- Temporary, non-sensitive project containing a README with the verification word ORCHID and one shared context file. No business repository was used.
- Project-level override to a working Anthropic-compatible model. User-wide Claude settings were not modified.

## Results

| Check | Result |
|---|---|
| Automated fake-CLI tests | Passed: submit/idempotence, scope and symlink refusal, edit refusal, question/answer, cancel, shutdown, review completion, retention |
| Codex-facing stdio MCP discovery | Passed: seven tools listed through an MCP client session |
| Local Codex registration | Passed: installed tool registered as an enabled stdio MCP server; installed executable listed seven tools |
| Wheel build with current pip | Passed: codex1cc 0.1.0 wheel |
| Linux sandbox boundary | Passed locally: selected README visible, unselected private file absent |
| Real read-only task | Passed: Claude read README, returned ORCHID, task entered review_required |
| Real blocking question | Passed: internal ask_codex tool asked once, answer returned to original round |
| Explicit session resume | Passed: second round used the saved session ID and returned ORCHID |
| Codex review completion | Passed: complete_task moved each reviewed task to completed |

The CLI reported a nonzero cumulative cost within the configured task limit. This is an observed CLI value, not a verified provider invoice. Each invocation also had its own cap.

## Release gates still open

- macOS process-level isolation and real-task acceptance have not been tested; real tasks fail closed there.
- At the time of this read-only validation, editing was disabled. The later native write experiment is recorded below.
- GitHub-hosted CI and installation on another user's machine have not yet been observed.
- The CLI cost report has not been compared with the provider invoice.

These results support a local Linux read-only alpha, not a complete cross-platform v1 release.

## Experimental automatic handoff (2026-09-29)

- Version 0.2.0 built as an sdist and wheel, installed locally with Python 3.10, and reported executor protocol version 2. The temporary handoff project's authorization and workspace were removed after the real test; the existing project configuration was preserved.
- The new SQLite outbox persists task events, host turn IDs, delivery receipts, Codex conclusions, and retry/attention states. State transitions and their handoff events are committed together. Automatic mode is opt-in per task and requires a prevalidated project binding.
- Automated tests: 23 passed locally, including the existing eight fake-CC tests, five fake end-to-end handoff tests, nine fake App Server protocol tests, and one database migration test. They cover completion, question then result, idempotent acknowledgement, explicit resolution of an uncertain turn, accepted-turn restart recovery, scoped MCP approval, host start/result correlation, busy-session event handling, and rejection of nonresumable sessions. No model request is made by these tests.
- Managed Codex App Server 0.158.0 on local Linux: three minimal turns in a temporary, non-sensitive workspace demonstrated legacy-session initialization, turn/start with a turn ID, cross-connection turn/completed, and final-result retrieval. The host API did not provide verified USD cost data; cost is unknown.
- `codex1cc bind PROJECT --create` initializes a dedicated legacy session with one real Codex model call. Binding an existing session only probes it. The normal doctor check does not call a model.
- A real, non-sensitive CC → Codex handoff passed: CC read a temporary README and reported `MAPLE`; the bound Codex turn called `get_task`, checked the acceptance condition, called `complete_task` and `ack_handoff`, and left a saved Codex conclusion. The task became `completed` and its handoff event became `handled`. `codex1cc watch` waited for program events and exited with the final record. An initial run exposed App Server MCP approval handling; after a task-scoped approval fix, a new event completed without redelivering the uncertain old one.
- Real structured-question handoff, managed daemon restart during an active turn, original-window display, real busy-session behavior, Linux outside this environment, macOS and native Windows remain unverified or unsupported as detailed in [host compatibility](handoff-compatibility.md). Automatic handoff is experimental, not a completed cross-platform v1 gate.

## Experimental native write backend (2026-09-29)

- The full fake-CLI suite passed **29 tests**, including project opt-in, a local commit on an isolated task branch, changed paths outside the declared task scope, retained partial edits after failure, and a write result delivered once to a fake bound Codex host and acknowledged after review. The original project working tree stayed unchanged.
- Three real, non-sensitive temporary Git-project runs used the installed Claude Code CLI and entered the native write backend. In each, CC changed `README.md` from `ALPHA` to `BETA` and created one local commit in the task worktree; the source checkout remained `ALPHA`, and the recorded changed-path list stayed within scope. The third run exited successfully, produced `claude_result` with `is_error=false`, and entered `review_required` with one commit.
- The first two real runs ended `failed/CLI_FAILED` after the commit; stderr included `unrecognized_model` for the configured model. A separate minimal `claude -p` call with the same model and budget exited successfully despite that warning, so the warning alone does not establish the failure cause. The executor retained the commit and worktree evidence. **Real structured question, interruption recovery, and automatic Codex acceptance for a write task remain unverified.**
- Write mode is an explicitly trusted project mode with command execution, not a sandbox. Path scope is checked at submission and reported against Git changes afterward; it cannot constrain arbitrary Bash access. Linux, WSL, macOS, and installation on another machine require separate write-backend acceptance before general support is claimed.
