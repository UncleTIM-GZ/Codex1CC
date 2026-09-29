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
    parser.add_argument("command", choices=["mcp", "doctor", "stop", "init-config", "config-path",
                                            "install-skill", "watch", "bind", "unbind"])
    parser.add_argument("target", nargs="?")
    parser.add_argument("thread_id", nargs="?")
    parser.add_argument("--create", action="store_true", help="Create a dedicated Codex thread (one model call)")
    parser.add_argument("--force", action="store_true", help="Replace managed files of an installed skill")
    args = parser.parse_args()
    if args.command == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return
    if args.command == "config-path":
        print(CONFIG)
        return
    if args.command == "install-skill":
        from .skill_install import install_skill
        try:
            target = install_skill(force=args.force)
            print(json.dumps({"skill": "codex1cc-ops", "path": str(target),
                              "installed": True}, ensure_ascii=False))
        except (BridgeError, OSError) as exc:
            code = exc.code if isinstance(exc, BridgeError) else "SKILL_INSTALL_FAILED"
            print(f"{code}: {exc}", file=sys.stderr)
            raise SystemExit(1)
        return
    if args.command == "watch":
        from .common import safe_id
        try:
            task_id = safe_id(args.target, "task_id")
            cursor = 0
            while True:
                report = asyncio.run(rpc("wait_task", {"task_id": task_id, "cursor": cursor}, timeout=40))
                for event in report["events"]:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                cursor = report["next_cursor"]
                task = report["task"]
                handoff = report["handoff"]
                counts = handoff.get("counts", {})
                latest = handoff.get("latest")
                waiting_for_handoff = (any(counts.get(state, 0) for state in
                                           ("pending", "sending", "accepted")) or
                                       bool(latest and latest["status"] == "handled" and
                                            latest["turn_id"] and latest["codex_status"] is None))
                if (task["status"] in {"review_required", "completed", "canceled", "failed", "interrupted"}
                        and not report["has_more"] and not waiting_for_handoff):
                    print(json.dumps({"task": task, "handoff": report["handoff"]}, ensure_ascii=False), flush=True)
                    break
        except KeyboardInterrupt:
            return
        except BridgeError as exc:
            print(f"{exc.code}: {exc}", file=sys.stderr)
            raise SystemExit(1)
        return
    if args.command == "bind":
        from .common import safe_id
        from .policy import project_config
        from .handoff_host import AppServerHost, HostError
        host = None
        thread_id = None
        try:
            project_id = safe_id(args.target, "project_id")
            project = project_config(project_id)
            backend = asyncio.run(rpc("doctor"))
            if backend.get("protocol_version", 0) < 2:
                raise BridgeError("HANDOFF_UNAVAILABLE", "Codex1CC executor must be upgraded and restarted")
            host = AppServerHost()
            if args.create:
                if args.thread_id:
                    raise BridgeError("INVALID_ARGUMENT", "Do not give a thread ID with --create")
                print("Creating a dedicated Codex thread; this uses one model call and may incur a charge.",
                      file=sys.stderr)
                thread_id = asyncio.run(host.initialize_thread(project["root"]))
            else:
                thread_id = args.thread_id
                if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 120:
                    raise BridgeError("INVALID_ARGUMENT", "Usage: codex1cc bind PROJECT_ID EXISTING_THREAD_ID or --create")
            capability = asyncio.run(host.probe({"thread_id": thread_id,
                                                "project_root": project["root"]}))
            if not capability.connected:
                raise HostError(capability.reason)
            document = json.loads(CONFIG.read_text(encoding="utf-8"))
            document["projects"][project_id]["handoff"] = {
                "mode": "automatic", "thread_id": thread_id,
                "max_turns": 3, "turn_seconds": 600}
            atomic_json(CONFIG, document)
            print(json.dumps({"project_id": project_id, "thread_id": thread_id,
                              "handoff": "connected"}, ensure_ascii=False))
        except (BridgeError, HostError, OSError, ValueError) as exc:
            print(f"HANDOFF_UNAVAILABLE: {exc}", file=sys.stderr)
            recovery_id = thread_id or (host.created_thread_id if host else None)
            if recovery_id:
                print(f"Created Codex thread ID for recovery: {recovery_id}", file=sys.stderr)
            raise SystemExit(1)
        return
    if args.command == "unbind":
        from .common import safe_id
        try:
            project_id = safe_id(args.target, "project_id")
            document = json.loads(CONFIG.read_text(encoding="utf-8"))
            entry = document.get("projects", {}).get(project_id)
            if not isinstance(entry, dict):
                raise BridgeError("PROJECT_NOT_ALLOWED", "Project is not configured")
            entry.pop("handoff", None)
            atomic_json(CONFIG, document)
            report = asyncio.run(rpc("disable_handoff", {"project_id": project_id}))
            print(json.dumps(report, ensure_ascii=False))
        except (BridgeError, OSError, ValueError) as exc:
            print(f"HANDOFF_UNAVAILABLE: {exc}", file=sys.stderr)
            raise SystemExit(1)
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
        if CONFIG.exists():
            from .common import projects
            from .handoff_host import AppServerHost
            report["handoff"] = {}
            for project_id, project in projects().items():
                if isinstance(project, dict) and isinstance(project.get("handoff"), dict):
                    binding = {**project["handoff"], "project_root": project.get("root")}
                    capability = asyncio.run(AppServerHost().probe(binding))
                    report["handoff"][project_id] = {
                        "connected": capability.connected, "reason": capability.reason,
                        "status": capability.status, "thread_id": capability.thread_id}
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except BridgeError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc), "state_path": str(STATE)},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
