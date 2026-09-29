"""Install the bundled Codex1CC operations skill for the current user."""

from __future__ import annotations

from importlib.resources import files
import os
from pathlib import Path

from .common import BridgeError


SKILL_NAME = "codex1cc-ops"
SKILL_FILES = ("SKILL.md", "agents/openai.yaml")


def install_skill(*, force: bool = False, root: Path | None = None) -> Path:
    """Install public skill files without replacing unrelated user content."""
    skills_root = root or Path.home() / ".agents" / "skills"
    target = skills_root / SKILL_NAME
    if skills_root.is_symlink() or target.is_symlink():
        raise BridgeError("INVALID_CONFIG", "Skill installation path must not be a symbolic link")

    source = files("codex1cc").joinpath("bundled_skills", SKILL_NAME)
    planned: list[tuple[str, str]] = []
    for relative in SKILL_FILES:
        content = source.joinpath(*relative.split("/")).read_text(encoding="utf-8")
        destination = target / relative
        if destination.is_symlink():
            raise BridgeError("INVALID_CONFIG", f"Skill file must not be a symbolic link: {destination}")
        if destination.exists() and destination.read_text(encoding="utf-8") != content and not force:
            raise BridgeError(
                "SKILL_EXISTS",
                f"A modified {SKILL_NAME} skill already exists; rerun with --force to replace its managed files",
            )
        planned.append((relative, content))

    target.mkdir(mode=0o755, parents=True, exist_ok=True)
    for relative, content in planned:
        destination = target / relative
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return target
