# Codex1CC

Codex1CC is a local MCP bridge that lets Codex submit one complete task to Claude Code CLI, then review its result during a later conversation. The executor stores tasks and events independently of the MCP connection. It does not poll Codex or wake a closed conversation.

**Status: alpha.** Linux has a read-only bubblewrap runner. A non-sensitive real task, blocking question, session resume, and Codex review passed locally. macOS isolation and broader platform acceptance are still release gates. Editing user projects is disabled. Do not use this as a security boundary for sensitive projects until those gates pass.

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

Create the shared context file inside the target project before submitting a task. Codex or the user maintains facts that should be reused across tasks. Claude receives it as read-only input. The selected read paths are copied into a private task snapshot; symbolic links and special files are rejected. The project configuration authorizes the maximum scope, and each task selects a subset.

Run diagnostics:

    codex1cc doctor

The doctor command starts the local executor if it is not running. It checks whether bubblewrap can launch; it does not prove that your model credentials or endpoint work.

Register the MCP server explicitly in Codex after reviewing the command:

    codex mcp add codex1cc -- /absolute/path/to/codex1cc mcp

Find that absolute executable path with command -v codex1cc. Use the path in the registration so a Codex client launched with a different PATH can still start the server.

Codex should submit one cohesive task with objective, task-specific context, acceptance checks, deliverables, path scope, time and budget limits, and a request ID. On the next natural interaction, call list_tasks, then get_task for the task needing an answer or review. Only complete_task after checking the actual result.

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
- **PROJECT_NOT_ALLOWED:** Check that the project ID exists, paths are relative to its root, the task scope is a subset of read_paths, and the shared file exists. Symlinks and special files are rejected.
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
