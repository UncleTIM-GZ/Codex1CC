# Codex1CC

Codex1CC lets you ask Codex to hand a well-defined task to Claude Code, then review the result with Codex later. You describe the task in plain language. Claude Code reads only the project files you authorize and returns its findings. The task record stays on the computer running the tool, so you can close Codex and check the result in a new conversation.

Install Codex1CC where it can access your project files and Claude Code. Codex connects to it through MCP, a tool interface; you do not need to run a web service. Codex1CC does not notify you when a task finishes or wake a closed conversation.

**Status: alpha.** Linux has a read-only bubblewrap runner. A non-sensitive real task, blocking question, session resume, and Codex review passed locally. macOS isolation and broader platform acceptance are still release gates. Editing user projects is disabled. Do not use this as a security boundary for sensitive projects until those gates pass.

**中文完整说明：**[安装、项目初始化、指挥 CC、跨会话验收与注意事项](README.zh-CN.md)。

Licensed under MIT; see [LICENSE](LICENSE).

## Requirements

- Python 3.10 or newer, a Codex client that supports local stdio MCP, and Claude Code CLI.
- Linux with bubblewrap for real read-only tasks. WSL uses the Linux path.
- macOS support is planned in the [PRD](Codex1CC%20产品需求文档.md); real tasks currently fail closed on macOS.
- Your own Claude Code authentication and model service. You are responsible for model charges.

## Install and configure

Install from a checkout using a Python tool environment:

    uv tool install .
    # or: pipx install .
    codex1cc init-config
    codex1cc config-path

Edit the displayed JSON config. Keep it readable only by your user (mode 0600). Example:

    {
      "projects": {
        "example": {
          "root": "/absolute/path/to/example",
          "shared_context": "CODEX1CC_CONTEXT.md",
          "read_paths": ["README.md", "src"],
          "model": "your-working-model-id",
          "limits": {"seconds": 1800, "usd": 0.25, "rounds": 3}
        }
      }
    }

Run `init-config` only once; it refuses to overwrite an existing configuration. Keep the JSON file private (`chmod 600 "$(codex1cc config-path)"`). The project ID must contain only ASCII letters, digits, `_`, or `-`, and `root` must be an existing absolute directory. `model` may be omitted if Claude Code's default model works; otherwise use a verified model ID. The configured `seconds`, `usd`, and `rounds` are project caps. A task can lower its time and USD caps, while the round cap comes from project configuration.

Create the shared context file inside the target project before submitting a task. Record authoritative document paths, stable constraints, and reusable public facts; exclude credentials and unverified status claims. Claude receives this file as read-only input even if it is not in the requested `scope`. The selected read paths are copied into a private task snapshot; symbolic links and special files are rejected. The project configuration authorizes the maximum scope, and each task selects a subset. Each task path must exactly match one configured `read_paths` entry: if `docs` is allowed, request `docs`, not an unlisted `docs/file.md`. Exclude generated assets and caches; a snapshot is limited to 20 MiB and 2000 files.

Run diagnostics:

    codex1cc doctor

The doctor command starts the background process if needed. It checks whether bubblewrap can launch; it does not prove that your model credentials or endpoint work.

Register the MCP server explicitly in Codex after reviewing the command:

    codex mcp add codex1cc -- /absolute/path/to/codex1cc mcp

Find that absolute executable path with command -v codex1cc. Use the path in the registration so a Codex client launched with a different PATH can still start the server.

Codex should submit one cohesive task with objective, task-specific context, acceptance checks, deliverables, path scope, time and budget limits, and a request ID. On the next natural interaction, call list_tasks, then get_task for the task needing an answer or review. Only complete_task after checking the actual result.

## Use Codex to direct Claude Code

Codex submits and reviews one complete task; Claude Code (CC) reads a bounded snapshot and returns findings. Speak to Codex in natural language. Codex uses the `codex1cc` MCP tools. Task state persists locally across Codex conversations. Codex1CC does not notify you, poll Codex, or wake a closed conversation.

### Start or re-enter an authorized project

Open the project workspace in your Codex client. In the CLI, start from the project directory:

```bash
cd /absolute/path/to/your-project
codex
```

The working directory does **not** select a Codex1CC project automatically. State the configured `project_id` in your request. If the MCP tools are missing, run `codex mcp list` and restart or refresh your Codex client. On another machine, create the project authorization first; this repository does not ship your local project configuration.

For a newly opened L21 session on the machine where L21 is configured, paste this prompt:

> I am managing `L21`. First check the current branch and working tree. Then call Codex1CC `list_tasks(project_id="L21", statuses=["queued","running","continuing","waiting_answer","review_required","failed","interrupted"])` once. Use `get_task` only for a question, review, or failure that needs action. Summarize the status and next step. Do not poll on a timer or resubmit an old task automatically.

Without `statuses`, `list_tasks` returns only tasks waiting for an answer or review, plus failed or interrupted tasks. Include running statuses explicitly when you want a complete unfinished-task view. The executor starts on demand; you do not have to launch it for each session.

### Submit one bounded task

Tell Codex the objective, task context, acceptance checks, deliverables, exact authorized paths, question policy, and limits. Codex supplies a unique `request_id` for each tool operation and reuses the same ID when retrying that operation. This release accepts only `actions=["read"]`. The project-level `rounds` cap is fixed by configuration; task-specific time and USD caps may be lower.

This prompt works on a computer where **L21 is already authorized with these paths**. Replace the project ID, paths, and objective for other projects:

> Use Codex1CC `submit_task` to send CC **one read-only task** with `project_id="L21"`. Objective: compare the Production Shell Migration plan with the current project documents and identify unresolved blockers, ordered by impact on the next decision. Context: follow `CLAUDE.md` and the baseline documents when deciding which source is authoritative; do not present old status notes as current facts. Set `scope=["README.md","CLAUDE.md","docs","openspec","project_chain/docs/baseline"]`. Acceptance: give a file path and evidence for each blocker; separate verified facts from items requiring verification; do not claim to have run tests or seen files outside the snapshot. Deliverables: a short conclusion, blocker list, and suggested first independent follow-up task. Ask me one concrete question only if blocked. Use `actions=["read"]`, at most 1800 seconds and 0.25 USD (rounds follow the project cap). Submit once, report the task ID and accepted scope, then end this turn. Do not wait or poll for the result.

Codex translates the prompt into `submit_task` arguments; you do not need to write JSON. CC can read only the snapshot and cannot execute project commands. Pick only the paths needed for this task. A snapshot over 20 MiB or 2000 files is rejected; narrow the task scope or add more specific authorized entries. Keep tightly coupled steps together. Split work only when the parts are independent and can actually run in parallel. Only one Codex1CC task can be active per project at a time.

### Return later, answer, review, or cancel

You may close the Codex conversation. Reopen Codex in the project or resume the old conversation, then use the session-start prompt above. The executor stores task IDs, status, original objective, acceptance checks, scope, and results independently of the chat. If you know a task ID, ask:

> Call Codex1CC `get_task(task_id="YOUR_TASK_ID")`. Show the original acceptance checks, current status, result, and any pending question. Do not submit a replacement task.

| Status | Action |
|---|---|
| `queued` / `running` / `continuing` | Save the task ID. Check again during a later natural interaction; do not poll on a timer. |
| `waiting_answer` | Read the question with `get_task`. After you answer, have Codex call `respond_task` within the original authorization. |
| `review_required` | Compare the result with the original acceptance checks. If it passes, call `complete_task` with a review note. If more investigation is needed and the original snapshot, budget, and rounds suffice, give `continue_task` a specific instruction. |
| `failed` / `interrupted` | Inspect the error and events. Fix the cause, then explicitly submit a new task. Interrupted work is never replayed automatically. |
| `completed` / `canceled` | Terminal states. History remains queryable until its retained content is pruned. |

Review prompt:

> Find L21 tasks awaiting review. Use `get_task` to compare each result with its saved acceptance checks and evidence. If evidence is missing, use `continue_task` only when the old snapshot and remaining budget suffice. Call `complete_task` only after review, then give me the final conclusion. Do not treat CC's statement that a test passed as a verified test run.

If an active task is no longer useful, ask Codex to call `cancel_task` for its ID. `complete_task` records acceptance; it does not edit or deploy the project. `continue_task` uses the original snapshot. Submit a new task to capture changed project files.

The task content for completed, failed, and canceled tasks is pruned after 30 days when the executor next starts. Pending questions and reviewable tasks are retained. For Codex startup and MCP registration, see the official [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) documentation.

### Agent operating contract

1. Use the explicit configured `project_id`; do not infer it from the current directory. On entering a session, call `list_tasks` once as needed and fetch only actionable tasks.
2. Submit each cohesive objective once, with complete context, exact allowed `scope`, acceptance checks, deliverables, question policy, and bounded cost/time. Generate a unique `request_id` per operation; preserve it on retry.
3. After `submit_task`, return the task ID and accepted status, then end the turn. Do not poll or hold the conversation open for CC.
4. On a later natural interaction, handle `waiting_answer` with `respond_task`, and `review_required` with evidence review followed by `continue_task` or `complete_task`. Never auto-replay `failed` or `interrupted` work.
5. Put reusable public facts in the project shared file; return concise conclusions and evidence. Delegate independent work separately only when it can run in parallel. CC cannot edit files or run project tests in this release.

## Authentication and safety

Restricted Claude sessions inherit only selected Anthropic authentication, endpoint and model environment fields from the process environment or the user's Claude settings. Global MCP servers, plugins and broad tool grants are not inherited. Credentials are never passed in command-line arguments or returned by the MCP tools.

Linux real tasks use bubblewrap to expose only a private read-only snapshot, Claude's own configuration and session data, the runtime needed for the internal question tool, and its local socket. The runner permits model-service network access but does not expose Claude command execution, WebFetch or browser tools. A missing sandbox fails the task. Project editing is not implemented.

The CLI budget flag limits a single invocation; previous rounds are tracked separately. If reported cost is missing, continuation fails closed. The CLI's figures may differ from your provider's actual bill.

Completed, failed and canceled task content is retained for 30 days, then pruned when the executor starts. Minimal task IDs and statuses remain. Tasks still waiting for an answer or review are not pruned. Claude's own session files are managed by Claude Code.

## Compatibility and release gates

| Environment | Automated tests | Read-only real task | Editing |
|---|---|---|---|
| Linux with bubblewrap | Passed locally | Passed locally with one read, one question, one resume | Disabled |
| WSL with bubblewrap | Same Linux code path | Passed locally on WSL; other WSL installations unverified | Disabled |
| macOS | Included in CI matrix | Fails closed until a verified isolation backend exists | Disabled |
| Native Windows | Not in v1 | Not supported | Not supported |

The local real test used a temporary project and a project-level model override. It made no changes to the user's global Claude settings or business repositories. macOS isolation and cross-machine compatibility remain required before a v1 release claim. CI runs fake-CLI tests and does not use model credentials.

See the [validation record](docs/validation.md) for the tested versions and remaining gates.

## Troubleshooting

- **SANDBOX_UNAVAILABLE:** Install bubblewrap on Linux and confirm unprivileged namespaces are permitted. On macOS, the isolation backend is not implemented yet.
- **PROJECT_NOT_ALLOWED:** Check that the project ID exists, every task scope entry exactly matches a configured project-relative `read_paths` entry, and the shared file exists. Symlinks and special files are rejected.
- **LIMIT_REACHED:** Narrow the scope, or check the configured time, USD, round, and snapshot limits.
- **CLI_FAILED:** Run codex1cc doctor, then check your Claude Code authentication, endpoint and model ID. A model rejected by its service is not a Codex1CC task success.
- **Task interrupted:** The executor restarted while a round was active. Review the snapshot and recorded events; it never replays an uncertain round automatically.
- **QUESTION_EXPIRED:** A blocking question remained unanswered for 24 hours. Inspect the saved task before deciding whether a session can be continued.
- **MCP server missing:** Verify the Codex registration with codex mcp list, then restart or refresh your Codex client. The stdio command is codex1cc mcp.

## Stop and uninstall

Use cancel_task for an active task. Stop the executor only after active tasks are resolved. Removing the Codex registration stops new MCP calls:

    codex1cc stop
    codex mcp remove codex1cc

After stopping active tasks, uninstall the Python package with the same tool used to install it. The configuration and state directories shown by doctor contain task history and are kept until you choose to remove them. Removing them is irreversible and does not modify any target project.

    uv tool uninstall codex1cc
    # or: pipx uninstall codex1cc

## Development and tests

    python3 -m pip install -e .
    python3 -m unittest discover -s tests -v

Use a current pip when building a wheel; older pip releases may ignore the project metadata. The automated tests use a fake Claude CLI and do not make model requests. See the [PRD](Codex1CC%20产品需求文档.md) for the full acceptance and release gates.
