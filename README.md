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

Tell Codex which authorized project you want to manage, such as L21. Opening a directory alone does not select a Codex1CC project. If the MCP tools are missing, run `codex mcp list` and restart or refresh your Codex client. On another machine, authorize the project first; this repository does not ship your project configuration.

For a newly opened L21 session on the machine where L21 is configured, paste this prompt:

> Continue with L21. Check the current branch and uncommitted changes, then see how the tasks I gave CC are progressing. Tell me if CC needs an answer or has a result ready for review. Do not submit the same task again or keep waiting for it to finish.

Codex checks task status for you; you do not need to remember tool names or status codes. The background process starts on demand, so there is nothing to launch for each session.

### Submit one bounded task

In plain language, tell Codex what you want to learn, which sources matter, what would count as a satisfactory answer, and what you want back. Mention any time or cost limit you have. Codex checks the authorized paths and fills in the tool arguments; you do not need to write JSON or memorize function names.

This prompt works on a computer where **L21 is already authorized with these paths**. Replace the project ID, paths, and objective for other projects:

> Ask CC to review L21's Production Shell Migration plan. Read the README, project rules, `docs`, `openspec`, and baseline documents. Identify unresolved blockers and rank them by impact on the next decision. Give a source path for each item and separate confirmed facts from questions. This is analysis only: do not edit files or claim to have run tests. Return a short conclusion and the first suggested follow-up task. Once delegated, give me the task number; we can review the result when I return.

Codex translates the request into a bounded task. CC can read only the selected snapshot and cannot execute project commands. A snapshot over 20 MiB or 2000 files is rejected; narrow the requested sources or authorize more specific paths. Keep tightly coupled steps together. Split work only when the parts are independent and can actually run in parallel. Only one Codex1CC task can be active per project at a time.

### Return later, answer, review, or cancel

You may close the Codex conversation. Reopen Codex in the project or resume the old conversation, then use the session-start prompt above. The executor stores task IDs, status, original objective, acceptance checks, scope, and results independently of the chat. If you know a task ID, ask:

> Check CC's earlier task, number "YOUR_TASK_ID". Tell me its progress, any question I need to answer, and its result. Do not submit it again.

| What you see | What to do |
|---|---|
| CC is waiting or working | Save the task number and ask Codex again later; do not keep checking. |
| CC has a question | Ask Codex to explain it, then give your answer for Codex to forward. |
| CC has returned a result | Ask Codex to review it against the original request; request more evidence if needed. |
| The task failed or stopped | Find out why, fix the cause, then explicitly ask for a new task. It is not retried automatically. |
| The task is complete or canceled | No action needed; its record remains queryable during the retention period. |

Review prompt:

> Help me review CC's result for L21. Check it against the original request and its evidence. If evidence is missing, tell me what is missing and ask CC to investigate further if possible. Record it as complete only when it meets the request, then give me the final conclusion. Do not treat CC's claim that a test passed as a verified test run.

If you no longer need a task, tell Codex to cancel it and give its task number. Recording acceptance does not edit or deploy the project. Follow-up investigation uses the original file snapshot; submit a new task to capture changed project files.

The task content for completed, failed, and canceled tasks is pruned after 30 days when the executor next starts. Pending questions and reviewable tasks are retained. For Codex startup and MCP registration, see the official [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) documentation.

### Agent operating contract

1. Resolve the user's project name to an explicitly configured `project_id`; do not infer it from the current directory alone. On entering a session, call `list_tasks(project_id=..., statuses=["queued","running","continuing","waiting_answer","review_required","failed","interrupted"])` once as needed and fetch only actionable tasks.
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
