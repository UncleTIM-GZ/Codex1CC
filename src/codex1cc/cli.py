"""User-facing diagnostics and MCP entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .common import CONFIG, STATE, BridgeError, atomic_json, rpc


def main() -> None:
    parser = argparse.ArgumentParser(prog="codex1cc")
    parser.add_argument("command", choices=["mcp", "doctor", "stop", "init-config", "config-path"])
    args = parser.parse_args()
    if args.command == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return
    if args.command == "config-path":
        print(CONFIG)
        return
    if args.command == "init-config":
        if CONFIG.exists():
            parser.error(f"Config already exists: {CONFIG}")
        atomic_json(CONFIG, {"projects": {}})
        print(CONFIG)
        return
    if args.command == "stop":
        try:
            asyncio.run(rpc("shutdown", autostart=False))
            print("Executor stopped")
        except BridgeError as exc:
            print(f"{exc.code}: {exc}", file=sys.stderr)
            raise SystemExit(1)
        return
    try:
        report = asyncio.run(rpc("doctor"))
        report["config_exists"] = CONFIG.exists()
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except BridgeError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc), "state_path": str(STATE)},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
