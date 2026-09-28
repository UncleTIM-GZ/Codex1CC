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
- Editing remains disabled on every platform.
- GitHub-hosted CI and installation on another user's machine have not yet been observed.
- The CLI cost report has not been compared with the provider invoice.

These results support a local Linux read-only alpha, not a complete cross-platform v1 release.
