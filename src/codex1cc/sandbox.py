"""Fail-closed filesystem isolation for Claude read-only jobs."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from .common import BridgeError, SOCKET, STATE


def linux_available() -> tuple[bool, str]:
    if sys.platform != "linux":
        return False, "Linux bubblewrap sandbox is unavailable on this platform"
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False, "bubblewrap is not installed"
    try:
        command = [bwrap, "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev"]
        for system_dir in ("/bin", "/lib", "/lib64"):
            if Path(system_dir).exists():
                command += ["--ro-bind", system_dir, system_dir]
        command += ["--", "/usr/bin/true"]
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode:
        return False, result.stderr.decode(errors="replace")[:500]
    return True, "bubblewrap available"


def _dirs(path: Path) -> list[Path]:
    result = []
    current = path
    while current != Path("/"):
        result.append(current)
        current = current.parent
    return list(reversed(result))


def wrap_linux(cli: str, command: list[str], snapshot: Path, mcp_config: Path) -> list[str]:
    ready, reason = linux_available()
    if not ready:
        raise BridgeError("SANDBOX_UNAVAILABLE", reason)
    home = Path.home().resolve()
    cli_real = Path(cli).resolve(strict=True)
    try:
        help_result = subprocess.run([str(cli_real), "--help"], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("CLI_FAILED", "Claude CLI capability check failed") from exc
    required = ("--print", "--output-format", "--restricted", "--strict-mcp-config",
                "--mcp-config", "--tools", "--allowedTools", "--permission-mode",
                "--resume")
    if help_result.returncode or any(flag not in help_result.stdout for flag in required):
        raise BridgeError("CLI_FAILED", "Claude CLI lacks a required capability")
    source_root = Path(__file__).resolve().parents[1]
    site_root = Path(__import__("mcp").__file__).resolve().parents[1]
    if not home.is_dir():
        raise BridgeError("SANDBOX_UNAVAILABLE", "Home directory is unavailable")
    if not SOCKET.exists() or not mcp_config.is_file():
        raise BridgeError("SANDBOX_UNAVAILABLE", "Question channel is unavailable")

    args = [shutil.which("bwrap"), "--die-with-parent", "--unshare-pid",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for system_dir in ("/bin", "/lib", "/lib64"):
        if Path(system_dir).exists():
            args += ["--ro-bind", system_dir, system_dir]
    mounts = [(cli_real, False), (source_root, False),
              (site_root, False), (snapshot, False), (mcp_config, False),
              (SOCKET, False)]
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")).expanduser()
    if config_dir.is_dir():
        mounts.append((config_dir.resolve(), True))
    if (home / ".claude.json").is_file():
        mounts.append((home / ".claude.json", False))
    for system_file in (Path("/etc/resolv.conf"), Path("/etc/hosts")):
        if system_file.is_symlink():
            target = system_file.resolve(strict=True)
            if not str(target).startswith("/etc/"):
                mounts.append((target, False))
    made: set[Path] = set()
    mounted: set[Path] = set()
    for source, writable in mounts:
        target = source
        if target in mounted:
            continue
        mounted.add(target)
        for directory in _dirs(target.parent):
            if directory in made or directory == Path("/"):
                continue
            # System trees already exist after their read-only mounts.
            if directory == Path("/tmp") or any(
                    directory == Path(base) or Path(base) in directory.parents
                    for base in ("/usr", "/etc", "/bin", "/lib", "/lib64")):
                made.add(directory)
                continue
            args += ["--dir", str(directory)]
            made.add(directory)
        args += ["--bind" if writable else "--ro-bind", str(source), str(target)]
    args += ["--chdir", str(snapshot),
             "--setenv", "HOME", str(home),
             "--setenv", "XDG_CACHE_HOME", "/tmp/cache",
             "--setenv", "XDG_STATE_HOME", "/tmp/state",
             "--", *command]
    return args
