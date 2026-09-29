"""Project authorization and read-only task snapshots."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat

from .common import BridgeError, projects, safe_id

MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
MAX_SNAPSHOT_FILES = 2000


def _open_regular(root_fd: int, parts: tuple[str, ...]) -> tuple[int, int]:
    """Open each path component relative to an already opened project root."""
    directory = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=directory)
            os.close(directory)
            directory = next_directory
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_fd)
            raise BridgeError("PROJECT_NOT_ALLOWED", "Only regular files may be copied")
        return file_fd, info.st_size
    finally:
        os.close(directory)


def project_config(project_id: str) -> dict:
    safe_id(project_id, "project_id")
    config = projects().get(project_id)
    if not isinstance(config, dict):
        raise BridgeError("PROJECT_NOT_ALLOWED", "Project is not configured")
    root_value = config.get("root")
    if not isinstance(root_value, str) or not root_value:
        raise BridgeError("INVALID_CONFIG", "Project root must be an absolute directory")
    root = Path(root_value).expanduser()
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise BridgeError("INVALID_CONFIG", "Project root must be an existing absolute directory")
    root = root.resolve(strict=True)
    paths = config.get("read_paths")
    shared = config.get("shared_context")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise BridgeError("INVALID_CONFIG", "read_paths must list allowed project-relative files or directories")
    if not isinstance(shared, str) or not shared:
        raise BridgeError("INVALID_CONFIG", "shared_context must name a project-relative file")
    for path in [*paths, shared]:
        validate_relative(path)
    write_backend = config.get("write_backend", {"enabled": False})
    if not isinstance(write_backend, dict) or type(write_backend.get("enabled", False)) is not bool:
        raise BridgeError("INVALID_CONFIG", "write_backend.enabled must be boolean")
    if any(key not in {"enabled", "write_paths"} for key in write_backend):
        raise BridgeError("INVALID_CONFIG", "Unknown write_backend setting")
    if write_backend.get("enabled"):
        from .workspace import validate_write_scope
        write_paths = write_backend.get("write_paths")
        validate_write_scope(write_paths, write_paths)
    model = config.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 100):
        raise BridgeError("INVALID_CONFIG", "model must be a nonempty model ID")
    limits = config.get("limits", {})
    if not isinstance(limits, dict):
        raise BridgeError("INVALID_CONFIG", "limits must be an object")
    context_policy = config.get("context_policy", {})
    if not isinstance(context_policy, dict) or any(
            key not in {"auto_compact_window", "auto_compact_percent"} for key in context_policy):
        raise BridgeError("INVALID_CONFIG", "Invalid context_policy settings")
    window = context_policy.get("auto_compact_window", 500000)
    percent = context_policy.get("auto_compact_percent", 70)
    if type(window) is not int or not 100000 <= window <= 1000000:
        raise BridgeError("INVALID_CONFIG", "context_policy.auto_compact_window must be 100000 through 1000000")
    if type(percent) is not int or not 1 <= percent <= 90:
        raise BridgeError("INVALID_CONFIG", "context_policy.auto_compact_percent must be 1 through 90")
    parallel = config.get("parallel", {})
    if not isinstance(parallel, dict) or any(key != "max_agents" for key in parallel):
        raise BridgeError("INVALID_CONFIG", "Invalid parallel settings")
    max_agents = parallel.get("max_agents", 3)
    if type(max_agents) is not int or not 1 <= max_agents <= 4:
        raise BridgeError("INVALID_CONFIG", "parallel.max_agents must be 1 through 4")
    cli = config.get("claude_path")
    if cli is not None and (not isinstance(cli, str) or not Path(cli).is_absolute() or not Path(cli).is_file()):
        raise BridgeError("INVALID_CONFIG", "claude_path must be an absolute executable file path")
    handoff = config.get("handoff")
    if handoff is not None:
        if not isinstance(handoff, dict) or handoff.get("mode") != "automatic":
            raise BridgeError("INVALID_CONFIG", "handoff must configure automatic mode")
        thread_id = handoff.get("thread_id")
        if not isinstance(thread_id, str) or len(thread_id) > 120 or not thread_id:
            raise BridgeError("INVALID_CONFIG", "handoff.thread_id must name a Codex thread")
        if any(key not in {"mode", "thread_id", "max_turns", "turn_seconds"} for key in handoff):
            raise BridgeError("INVALID_CONFIG", "Unknown handoff setting; a hard USD cap is not supported")
        if type(handoff.get("max_turns", 3)) is not int or not 1 <= handoff.get("max_turns", 3) <= 10:
            raise BridgeError("INVALID_CONFIG", "handoff.max_turns must be 1 through 10")
        if type(handoff.get("turn_seconds", 600)) is not int or not 30 <= handoff.get("turn_seconds", 600) <= 3600:
            raise BridgeError("INVALID_CONFIG", "handoff.turn_seconds must be 30 through 3600")
    return {"root": str(root), "read_paths": paths, "shared_context": shared,
            "limits": limits, "claude_path": config.get("claude_path"),
            "model": model, "handoff": handoff, "write_backend": write_backend,
            "context_policy": {"auto_compact_window": window, "auto_compact_percent": percent},
            "parallel": {"max_agents": max_agents}}


def validate_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in ("..", ".git") for part in path.parts):
        raise BridgeError("INVALID_ARGUMENT", f"Unsafe project path: {value}")
    return path


def snapshot(project: dict, requested: list[str], destination: Path) -> tuple[str, dict]:
    """Copy only authorized regular files; symlinks and special files fail closed."""
    allowed = set(project["read_paths"])
    if not requested or not set(requested).issubset(allowed):
        raise BridgeError("PROJECT_NOT_ALLOWED", "Task paths must be a nonempty subset of read_paths")
    chosen = sorted(set(requested) | {project["shared_context"]})
    root = Path(project["root"])
    total = count = 0
    copied: set[Path] = set()
    digest = hashlib.sha256()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    root_fd = None
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for name in chosen:
            relative = validate_relative(name)
            source = root / relative
            if any((root / Path(*relative.parts[:index])).is_symlink()
                   for index in range(1, len(relative.parts) + 1)) or not source.exists():
                raise BridgeError("PROJECT_NOT_ALLOWED", f"Unavailable or linked path: {name}")
            files = [source] if source.is_file() else list(source.rglob("*"))
            for file in files:
                if file.is_dir():
                    continue
                if file.is_symlink() or not file.is_file() or ".git" in file.relative_to(root).parts:
                    raise BridgeError("PROJECT_NOT_ALLOWED", f"Unsafe file: {file.relative_to(root)}")
                if file in copied:
                    continue
                copied.add(file)
                digest.update(str(file.relative_to(root)).encode())
                file_fd, file_size = _open_regular(root_fd, file.relative_to(root).parts)
                count += 1
                total += file_size
                if count > MAX_SNAPSHOT_FILES or total > MAX_SNAPSHOT_BYTES:
                    os.close(file_fd)
                    raise BridgeError("LIMIT_REACHED", "Project snapshot is too large")
                target = destination / file.relative_to(root)
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with os.fdopen(file_fd, "rb") as source_stream, target.open("xb") as target_stream:
                        file_fd = -1
                        copied_size = 0
                        while chunk := source_stream.read(65536):
                            copied_size += len(chunk)
                            if total - file_size + copied_size > MAX_SNAPSHOT_BYTES:
                                raise BridgeError("LIMIT_REACHED", "Project snapshot is too large")
                            target_stream.write(chunk)
                            digest.update(chunk)
                        total = total - file_size + copied_size
                finally:
                    if file_fd >= 0:
                        os.close(file_fd)
                os.chmod(target, 0o400)
        os.chmod(destination, 0o500)
        return digest.hexdigest(), {"files": count, "bytes": total, "paths": chosen}
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if root_fd is not None:
            os.close(root_fd)
