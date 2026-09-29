"""Git worktrees for explicitly trusted Claude editing tasks."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

from .common import BridgeError, STATE, private_dir
from .policy import validate_relative


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("GIT_UNAVAILABLE", "Git command could not run") from exc
    if result.returncode:
        raise BridgeError("GIT_FAILED", (result.stderr or "Git command failed")[:500])
    return result.stdout.rstrip("\n")


def _inside(path: str, allowed: list[str]) -> bool:
    return any(entry == "." or path == entry or path.startswith(entry.rstrip("/") + "/")
               for entry in allowed)


def cli_ready(cli: str) -> None:
    try:
        result = subprocess.run([cli, "--help"], capture_output=True, text=True,
                                timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("CLI_FAILED", "Claude CLI capability check failed") from exc
    required = ("--print", "--output-format", "--permission-mode", "--tools",
                "--max-budget-usd", "--strict-mcp-config", "--resume")
    if result.returncode or any(flag not in result.stdout for flag in required):
        raise BridgeError("CLI_FAILED", "Claude CLI lacks native write requirements")


def project_ready(project: dict) -> None:
    if sys.platform not in {"linux", "darwin"}:
        raise BridgeError("SANDBOX_UNAVAILABLE", "Native write backend supports POSIX Linux and macOS only")
    if not project.get("write_backend", {}).get("enabled"):
        raise BridgeError("SANDBOX_UNAVAILABLE", "Native write backend is not enabled")
    if not shutil.which("git"):
        raise BridgeError("GIT_UNAVAILABLE", "Git is unavailable")
    root = Path(project["root"])
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise BridgeError("INVALID_CONFIG", "Write backend root must be the Git repository top level")
    _git(root, "rev-parse", "HEAD")
    cli_ready(project.get("claude_path") or shutil.which("claude") or "")


def validate_write_scope(configured: list[str], requested: list[str]) -> list[str]:
    if not isinstance(configured, list) or not configured or any(
            not isinstance(value, str) for value in configured):
        raise BridgeError("INVALID_CONFIG", "write_paths must list project-relative paths")
    for value in configured:
        if value != ".":
            validate_relative(value)
    if not isinstance(requested, list) or not requested or any(
            not isinstance(value, str) for value in requested):
        raise BridgeError("INVALID_ARGUMENT", "Task write scope must list relative paths")
    for value in requested:
        if value != ".":
            validate_relative(value)
        if not _inside(value, configured):
            raise BridgeError("PROJECT_NOT_ALLOWED", "Task write scope exceeds write_paths")
    return sorted(set(requested))


def create_worktree(root_value: str, task_id: str, shared_context: str) -> dict:
    root = Path(root_value)
    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root.resolve():
        raise BridgeError("INVALID_CONFIG", "Write backend root must be the Git repository top level")
    base = _git(root, "rev-parse", "HEAD")
    dirty_source = bool(_git(root, "status", "--porcelain", "--untracked-files=normal"))
    worktrees = STATE / "worktrees"
    private_dir(worktrees)
    target = worktrees / task_id
    if target.resolve().is_relative_to(root.resolve()):
        raise BridgeError("INVALID_CONFIG", "Task state must be outside the source Git repository")
    if target.exists() or target.is_symlink():
        raise BridgeError("INVALID_STATE", "Task worktree path already exists")
    branch = f"codex1cc/{task_id}"
    _git(root, "worktree", "add", "-b", branch, str(target), base)
    try:
        shared = root / validate_relative(shared_context)
        if shared.is_symlink() or not shared.is_file():
            raise BridgeError("PROJECT_NOT_ALLOWED", "Shared context must be a regular file")
        content = shared.read_bytes()
        shared_dir = STATE / "shared"
        private_dir(shared_dir)
        saved = shared_dir / f"{task_id}.txt"
        saved.write_bytes(content)
        saved.chmod(0o400)
    except Exception:
        _git(root, "worktree", "remove", "--force", str(target))
        _git(root, "branch", "-D", branch)
        raise
    return {"backend": "native_write", "worktree_path": str(target), "branch": branch,
            "base_commit": base, "source_dirty": dirty_source,
            "shared_context_snapshot": str(saved),
            "shared_context_sha256": hashlib.sha256(content).hexdigest()}


def discard_unstarted(root_value: str, workspace: dict, task_id: str) -> None:
    root = Path(root_value)
    _git(root, "worktree", "remove", "--force", workspace["worktree_path"])
    _git(root, "branch", "-D", workspace["branch"])
    (STATE / "shared" / f"{task_id}.txt").unlink(missing_ok=True)


def verify_worktree(root_value: str, workspace: dict) -> Path:
    path = Path(workspace["worktree_path"])
    expected = (STATE / "worktrees" / path.name).resolve()
    if path.resolve() != expected or not path.is_dir() or path.is_symlink():
        raise BridgeError("INVALID_STATE", "Managed worktree is missing or changed")
    if Path(_git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
        raise BridgeError("INVALID_STATE", "Worktree root changed")
    if _git(path, "branch", "--show-current") != workspace["branch"]:
        raise BridgeError("INVALID_STATE", "Task branch changed")
    # Git reports the common directory using a mix of absolute and relative paths.
    common = lambda directory: (directory / _git(directory, "rev-parse", "--git-common-dir")).resolve()
    if common(path) != common(Path(root_value)):
        raise BridgeError("INVALID_STATE", "Worktree repository changed")
    return path


def artifacts(root_value: str, workspace: dict, scope: list[str]) -> dict:
    path = verify_worktree(root_value, workspace)
    base = workspace["base_commit"]
    head = _git(path, "rev-parse", "HEAD")
    commits = _git(path, "log", "--format=%H %s", f"{base}..HEAD").splitlines()
    changed = set(filter(None, _git(path, "diff", "--no-renames", "--name-only", "-z", base).split("\0")))
    changed.update(filter(None, _git(path, "ls-files", "-z", "--others", "--exclude-standard").split("\0")))
    dirty = bool(_git(path, "status", "--porcelain", "--untracked-files=normal"))
    outside = sorted(p for p in changed if not _inside(p, scope))
    return {**workspace, "head_commit": head, "commits": commits[:50],
            "commits_truncated": len(commits) > 50,
            "changed_paths": sorted(changed)[:50], "changed_paths_truncated": len(changed) > 50,
            "outside_scope": outside[:50], "outside_scope_truncated": len(outside) > 50,
            "dirty": dirty}
