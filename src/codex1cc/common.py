"""Paths, validation and local IPC shared by all entry points."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import sys
from contextlib import contextmanager

def _state_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex1cc"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "codex1cc"


def _config_file() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "codex1cc" / "projects.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "codex1cc" / "projects.json"


STATE = Path(os.environ.get("CODEX1CC_STATE_DIR", _state_dir()))
CONFIG = Path(os.environ.get("CODEX1CC_CONFIG", _config_file()))
SOCKET = STATE / "daemon.sock"
MAX_LINE = 128 * 1024
ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@contextmanager
def executor_lock():
    """Own the state before opening SQLite or inspecting/removing its socket."""
    import fcntl

    private_dir(STATE)
    fd = os.open(STATE / "daemon.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeError("DAEMON_RUNNING", "Executor already owns this state directory") from exc
        yield
    finally:
        os.close(fd)


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise BridgeError("INVALID_CONFIG", f"State directory is a symbolic link: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise BridgeError("INVALID_CONFIG", f"State directory owner is invalid: {path}")
    os.chmod(path, 0o700)


def atomic_json(path: Path, data: object) -> None:
    private_dir(path.parent)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise BridgeError("INVALID_ARGUMENT", f"Invalid {field}")
    return value


def projects() -> dict:
    if not CONFIG.exists():
        return {}
    if CONFIG.is_symlink() or not CONFIG.is_file() or CONFIG.stat().st_uid != os.getuid() or CONFIG.stat().st_mode & 0o077:
        raise BridgeError("INVALID_CONFIG", "Project config owner or type is invalid")
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError("INVALID_CONFIG", str(exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        raise BridgeError("INVALID_CONFIG", "Expected a projects object")
    return data["projects"]


async def rpc(method: str, params: dict | None = None, *, autostart: bool = True, timeout: float = 25) -> dict:
    if autostart and not SOCKET.exists():
        await start_daemon()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(SOCKET), limit=4 * 1024 * 1024), timeout=5)
    except (OSError, asyncio.TimeoutError):
        if not autostart:
            raise BridgeError("DAEMON_UNAVAILABLE", "Executor is not running")
        await start_daemon()
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(SOCKET), limit=4 * 1024 * 1024), timeout=5)
    try:
        payload = json.dumps({"method": method, "params": params or {}}, ensure_ascii=False).encode() + b"\n"
        if len(payload) > MAX_LINE:
            raise BridgeError("INVALID_ARGUMENT", "Request is too large")
        writer.write(payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not raw:
            raise BridgeError("DAEMON_UNAVAILABLE", "Executor closed the connection")
        response = json.loads(raw)
        if not response.get("ok"):
            error = response.get("error", {})
            raise BridgeError(error.get("code", "INTERNAL_ERROR"), error.get("message", "Unknown error"))
        return response["data"]
    finally:
        writer.close()
        await writer.wait_closed()


async def start_daemon() -> None:
    import subprocess

    private_dir(STATE)
    if SOCKET.exists():
        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET), limit=4 * 1024 * 1024)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            pass
    log = STATE / "daemon.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab", buffering=0) as stream:
        subprocess.Popen([sys.executable, "-m", "codex1cc.daemon"],
                         stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                         close_fds=True, start_new_session=True)
    for _ in range(100):
        await asyncio.sleep(0.05)
        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET))
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            pass
    raise BridgeError("DAEMON_UNAVAILABLE", f"Executor did not start; inspect {log}")
