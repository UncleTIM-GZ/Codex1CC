"""Durable local task executor. The MCP frontend is intentionally disposable."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import time
import uuid

from .common import BridgeError, MAX_LINE, SOCKET, STATE, atomic_json, private_dir, projects, safe_id
from .auth import provider_environment
from .policy import project_config, snapshot
from .sandbox import linux_available, wrap_linux
from .store import Store, scrub
from .workspace import (artifacts, create_worktree, discard_unstarted, project_ready,
                        validate_write_scope, verify_worktree)

ACTIONABLE = {"waiting_answer", "review_required", "failed", "interrupted"}
KNOWN = ACTIONABLE | {"queued", "running", "continuing", "completed", "canceled"}


class Executor:
    def __init__(self, shutdown_event: asyncio.Event | None = None) -> None:
        self.store = Store()
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.answers: dict[str, asyncio.Future] = {}
        self.stopping = False
        self.shutdown_event = shutdown_event
        self.handoff_changed = asyncio.Event()
        self.activity_changed = asyncio.Event()
        self.store.mark_interrupted()
        self.store.prune_terminal_history()
        self.store.on_event = self.activity_changed.set

    async def dispatch(self, method: str, params: dict) -> dict:
        if method == "submit_task":
            return await self.submit(params)
        if method == "list_tasks":
            project = params.get("project_id")
            if project is not None:
                safe_id(project, "project_id")
            statuses = params.get("statuses")
            if statuses is None:
                statuses = sorted(ACTIONABLE)
            if not isinstance(statuses, list) or any(s not in KNOWN for s in statuses):
                raise BridgeError("INVALID_ARGUMENT", "Invalid statuses")
            offset = self._number(params.get("offset", 0), 0, 100000)
            limit = self._number(params.get("limit", 20), 1, 100)
            tasks = self.store.list_tasks(project, statuses, offset, limit)
            for task in tasks:
                task["handoff"] = self.store.handoff_status(task["id"])
            return {"tasks": tasks, "next_offset": offset + limit}
        if method == "get_task":
            task_id = safe_id(params.get("task_id"), "task_id")
            item = self.store.one(task_id)
            cursor = self._number(params.get("cursor", 0), 0, 2**63 - 1)
            include_events = params.get("include_events", False)
            if not isinstance(include_events, bool):
                raise BridgeError("INVALID_ARGUMENT", "include_events must be boolean")
            events, more = self.store.events(task_id, cursor) if include_events else ([], False)
            public = self._public_task(item)
            public["handoff"] = self.store.handoff_status(task_id)
            return {"task": public, "questions": self.store.pending_questions(task_id),
                    "events": events, "next_cursor": events[-1]["seq"] if events else cursor,
                    "has_more": more}
        if method == "wait_task":
            task_id = safe_id(params.get("task_id"), "task_id")
            cursor = self._number(params.get("cursor", 0), 0, 2**63 - 1)
            while True:
                item = self.store.one(task_id)
                events, more = self.store.events(task_id, cursor)
                handoff = self.store.handoff_status(task_id)
                if events or self._watch_finished(item, handoff):
                    return {"task": self._public_task(item), "handoff": handoff,
                            "events": events, "next_cursor": events[-1]["seq"] if events else cursor,
                            "has_more": more}
                self.activity_changed.clear()
                try:
                    await asyncio.wait_for(self.activity_changed.wait(), timeout=30)
                except asyncio.TimeoutError:
                    return {"task": self._public_task(item), "handoff": handoff,
                            "events": [], "next_cursor": cursor, "has_more": False}
        if method == "respond_task":
            return self.respond(params)
        if method == "continue_task":
            return await self.continue_task(params)
        if method == "complete_task":
            return self.complete(params)
        if method == "cancel_task":
            return await self.cancel(params)
        if method == "ack_handoff":
            return self.ack_handoff(params)
        if method == "disable_handoff":
            project_id = safe_id(params.get("project_id"), "project_id")
            count = self.store.handoff_suspend_project(project_id)
            self.handoff_changed.set()
            return {"project_id": project_id, "disabled_pending_events": count}
        if method == "ask_question":
            return await self.ask(params)
        if method == "doctor":
            return self.doctor()
        if method == "shutdown":
            active = self.store.list_tasks(None, ["queued", "running", "waiting_answer", "continuing"], 0, 1)
            if active:
                raise BridgeError("INVALID_STATE", "Cancel active tasks before stopping the executor")
            if self.shutdown_event:
                self.shutdown_event.set()
            return {"stopping": True}
        raise BridgeError("INVALID_ARGUMENT", f"Unknown method: {method}")

    @staticmethod
    def _number(value: object, low: int, high: int) -> int:
        if type(value) is not int or not low <= value <= high:
            raise BridgeError("INVALID_ARGUMENT", f"Expected integer between {low} and {high}")
        return value

    @staticmethod
    def _text(value: object, name: str, maximum: int = 10000) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise BridgeError("INVALID_ARGUMENT", f"Invalid {name}")
        return value.strip()

    def _request_id(self, params: dict, method: str) -> tuple[str, dict | None]:
        request_id = safe_id(params.get("request_id"), "request_id")
        return request_id, self.store.operation(request_id, method)

    @staticmethod
    def _scope_conflict(left: list[str], right: list[str]) -> bool:
        for a in left:
            for b in right:
                if a == "." or b == ".":
                    return True
                a_parts, b_parts = Path(a).parts, Path(b).parts
                if a_parts[:len(b_parts)] == b_parts or b_parts[:len(a_parts)] == a_parts:
                    return True
        return False

    def _admit_parallel(self, project_id: str, project: dict, bundle: dict,
                        exclude: str | None = None) -> None:
        active = self.store.active(project_id, exclude)
        if not active:
            return
        maximum = project.get("parallel", {}).get("max_agents", 3)
        if not bundle.get("parallel_ok") or maximum <= 1 or len(active) >= maximum:
            raise BridgeError("PROJECT_BUSY", "Project agent limit reached or task is serial")
        for other in active:
            existing = other["bundle"]
            if (not existing.get("parallel_ok") or
                    self._scope_conflict(bundle["scope"], existing.get("scope", []))):
                raise BridgeError("PROJECT_BUSY", "Task scope conflicts with an active task; integrate its baseline before submitting")

    def ack_handoff(self, params: dict) -> dict:
        event_id = safe_id(params.get("event_id"), "event_id")
        receipt_token = params.get("receipt_token")
        if not isinstance(receipt_token, str) or len(receipt_token) > 200:
            raise BridgeError("INVALID_ARGUMENT", "Invalid handoff receipt token")
        outcome = params.get("outcome")
        if not isinstance(outcome, str) or outcome not in {
                "completed", "answered", "continued", "needs_user", "reviewed", "failed"}:
            raise BridgeError("INVALID_ARGUMENT", "Invalid handoff outcome")
        event = self.store.handoff_one(event_id)
        if not hmac.compare_digest(receipt_token, event["receipt_token"]):
            raise BridgeError("INVALID_STATE", "Handoff receipt does not match the delivered turn")
        task = self.store.one(event["task_id"])
        expected = {"completed": "completed", "continued": "continuing"}
        if outcome in expected and task["status"] != expected[outcome] and not (
                outcome == "continued" and task["status"] == "running"):
            raise BridgeError("INVALID_STATE", "Task state does not match handoff outcome")
        if outcome == "answered" and event["kind"] != "question":
            raise BridgeError("INVALID_STATE", "Only a question can be acknowledged as answered")
        if outcome == "answered" and any(q["id"] == event["summary"].get("question_id")
                                          for q in self.store.pending_questions(event["task_id"])):
            raise BridgeError("INVALID_STATE", "Question still needs an answer")
        if outcome == "reviewed" and (event["kind"] not in {"review_required", "question_expired"}
                                       or task["status"] != "review_required"):
            raise BridgeError("INVALID_STATE", "Task is not awaiting this review")
        item = self.store.handoff_handle(event_id, event["turn_id"], outcome)
        self.store.event(event["task_id"], "handoff_handled", {"event_id": event_id, "outcome": outcome})
        self.handoff_changed.set()
        return {"event_id": event_id, "status": item["status"], "outcome": item["outcome"]}

    @staticmethod
    def _handoff_current(event: dict, task: dict) -> bool:
        kind, status = event["kind"], task["status"]
        if kind == "question":
            return status == "waiting_answer"
        if kind in {"review_required", "question_expired"}:
            return status == "review_required"
        return status == kind

    @staticmethod
    def _watch_finished(task: dict, handoff: dict) -> bool:
        if task["status"] not in {"review_required", "completed", "canceled", "failed", "interrupted"}:
            return False
        counts = handoff.get("counts", {})
        if any(counts.get(state, 0) for state in ("pending", "sending", "accepted")):
            return False
        latest = handoff.get("latest")
        return not (latest and latest["status"] == "handled" and latest["turn_id"]
                    and latest["codex_status"] is None)

    @staticmethod
    def _handoff_prompt(event: dict) -> str:
        return (
            "Codex1CC 任务事件。请处理这一个事件，不要轮询或重交旧任务。\n"
            f"event_id={event['id']} task_id={event['task_id']} kind={event['kind']} "
            f"round_no={event['round_no']} receipt_token={event['receipt_token']}。\n"
            "先用 get_task 读取当前任务与原验收条件，并核对状态。只按需读取详细事件或产物。"
            "只在真实产品取舍或超出项目授权时请求用户裁定；不要为 CC 费用提问。"
            "处理结束时调用 ack_handoff(event_id, receipt_token, outcome)，其中 outcome 为 "
            "completed、answered、continued、needs_user、reviewed 或 failed。"
            "向用户给出简短结论和下一步。"
        )

    async def handoff_loop(self) -> None:
        """Wait for stored events or a transport retry deadline; no model polling."""
        from .handoff_host import AppServerHost
        host = AppServerHost()
        watchers: set[asyncio.Task] = set()

        def scoped_binding(event: dict) -> dict:
            return {**event["binding"], "_handoff_task_id": event["task_id"],
                    "_handoff_event_id": event["id"],
                    "_handoff_receipt_token": event["receipt_token"]}

        async def reconcile_uncertain(event: dict, reason: str) -> None:
            try:
                found = await host.reconcile_event(event["binding"], event["id"])
                if found.turn_id:
                    self.store.handoff_accept(event["id"], found.turn_id)
                    if found.status == "inProgress":
                        watch(event["id"], scoped_binding(event), found.turn_id)
                    elif self.store.handoff_one(event["id"])["status"] == "accepted":
                        self.store.handoff_flag(event["id"], "Codex turn ended without a handling receipt")
                else:
                    self.store.handoff_flag(event["id"], f"Uncertain delivery: {reason}")
            except Exception as exc:
                self.store.handoff_flag(event["id"], f"Uncertain delivery: {reason}; reconcile failed: {exc}")

        def watch(event_id: str, binding: dict, turn_id: str) -> None:
            async def run() -> None:
                try:
                    delivery = await host.wait_for_turn(
                        binding, turn_id, timeout=binding.get("turn_seconds", 600))
                    if (delivery.status == "unknown" and delivery.error and
                            ("Timed out" in delivery.error or "Interactive request" in delivery.error)):
                        try:
                            await host.interrupt_turn(binding, turn_id)
                            delivery = await host.wait_for_turn(binding, turn_id, timeout=15)
                            if delivery.status == "inProgress":
                                delivery = await host.reconcile(binding, turn_id)
                        except Exception:
                            pass
                    if delivery.status in {"completed", "failed", "interrupted", "unknown"}:
                        self.store.handoff_record_turn(event_id, delivery.status, delivery.result)
                        self.store.event(self.store.handoff_one(event_id)["task_id"], "codex_handoff_result",
                                         {"event_id": event_id, "status": delivery.status,
                                          "conclusion": delivery.result})
                        if self.store.handoff_one(event_id)["status"] == "accepted":
                            self.store.handoff_flag(event_id, "Codex turn ended without a handling receipt")
                            self.handoff_changed.set()
                    elif delivery.status == "inProgress" and self.store.handoff_one(event_id)["status"] == "accepted":
                        self.store.handoff_flag(event_id, "Codex turn remained active after the time limit")
                        self.handoff_changed.set()
                except Exception as exc:
                    if self.store.handoff_one(event_id)["status"] == "accepted":
                        self.store.handoff_flag(event_id, f"Cannot observe Codex turn: {exc}")
                        self.handoff_changed.set()

            job = asyncio.create_task(run())
            watchers.add(job)
            job.add_done_callback(watchers.discard)

        def watch_idle(event_id: str, binding: dict) -> None:
            async def run() -> None:
                try:
                    capability = await host.wait_for_idle(binding)
                    if capability.connected and capability.status == "idle":
                        self.store.handoff_awaken(event_id)
                        self.handoff_changed.set()
                except Exception:
                    pass  # The bounded program timer remains a fallback.

            job = asyncio.create_task(run())
            watchers.add(job)
            job.add_done_callback(watchers.discard)

        for event in self.store.handoff_sending():
            await reconcile_uncertain(event, "Executor restarted during handoff delivery")

        for event in self.store.handoff_accepted():
            try:
                delivery = await host.reconcile(event["binding"], event["turn_id"])
                if delivery.status in {"completed", "failed", "interrupted"}:
                    self.store.handoff_record_turn(event["id"], delivery.status, delivery.result)
                    if self.store.handoff_one(event["id"])["status"] == "accepted":
                        self.store.handoff_flag(event["id"], "Codex turn ended without a handling receipt")
                elif delivery.status == "unknown":
                    self.store.handoff_flag(event["id"], "Unable to reconcile prior Codex turn")
                else:
                    watch(event["id"], scoped_binding(event), event["turn_id"])
            except Exception as exc:
                self.store.handoff_flag(event["id"], f"Unable to reconcile prior Codex turn: {exc}")
        for event in self.store.handoff_unrecorded():
            try:
                delivery = await host.reconcile(event["binding"], event["turn_id"])
                if delivery.status in {"completed", "failed", "interrupted"}:
                    self.store.handoff_record_turn(event["id"], delivery.status, delivery.result)
            except Exception:
                pass
        while not self.stopping:
            ready = self.store.handoff_ready()
            busy = self.store.handoff_busy_threads()
            processed = False
            for event in ready:
                thread_id = event["binding"].get("thread_id", "")
                if thread_id in busy:
                    continue
                task = self.store.one(event["task_id"])
                try:
                    current_binding = projects().get(task["project_id"], {}).get("handoff")
                except BridgeError as exc:
                    self.store.handoff_defer(event["id"], str(exc), 30)
                    processed = True
                    continue
                if (not isinstance(current_binding, dict) or current_binding.get("mode") != "automatic"
                        or current_binding.get("thread_id") != thread_id):
                    self.store.handoff_suspend_project(task["project_id"])
                    self.store.event(event["task_id"], "handoff_disabled",
                                     {"event_id": event["id"], "reason": "Project binding changed"})
                    processed = True
                    continue
                max_turns = min(event["binding"].get("max_turns", 3),
                                current_binding.get("max_turns", 3))
                if self.store.handoff_started_count(event["task_id"]) >= max_turns:
                    self.store.handoff_stop(event["id"], "Codex handoff turn limit reached")
                    self.store.event(event["task_id"], "handoff_disabled",
                                     {"event_id": event["id"], "reason": "Codex handoff turn limit reached"})
                    processed = True
                    continue
                question_pending = (event["kind"] != "question" or any(
                    q["id"] == event["summary"].get("question_id")
                    for q in self.store.pending_questions(event["task_id"])))
                if not self._handoff_current(event, task) or not question_pending:
                    self.store.handoff_supersede(event["id"])
                    processed = True
                    continue
                try:
                    capability = await host.probe(event["binding"])
                    if not capability.connected:
                        self.store.handoff_defer(event["id"], capability.reason or "Codex host unavailable", 30)
                        processed = True
                        continue
                    if not self.store.handoff_begin(event["id"]):
                        processed = True
                        continue
                    delivery = await host.deliver(scoped_binding(event), event["id"],
                                                  self._handoff_prompt(event))
                    if delivery.turn_id:
                        self.store.handoff_accept(event["id"], delivery.turn_id)
                        self.store.event(event["task_id"], "handoff_accepted",
                                         {"event_id": event["id"], "turn_id": delivery.turn_id})
                        busy.add(thread_id)
                        if delivery.status == "inProgress":
                            effective = {**scoped_binding(event), "turn_seconds": min(
                                event["binding"].get("turn_seconds", 600),
                                current_binding.get("turn_seconds", 600))}
                            watch(event["id"], effective, delivery.turn_id)
                        else:
                            self.store.handoff_record_turn(event["id"], delivery.status, delivery.result)
                            if self.store.handoff_one(event["id"])["status"] == "accepted":
                                self.store.handoff_flag(event["id"], "Codex turn ended without a handling receipt")
                    elif delivery.status == "busy":
                        self.store.handoff_defer(event["id"], "Codex session is busy", 3600)
                        watch_idle(event["id"], event["binding"])
                    else:
                        if delivery.status == "unknown":
                            await reconcile_uncertain(event, delivery.error or "unknown result")
                        else:
                            self.store.handoff_retry(event["id"], delivery.error or delivery.status)
                except Exception as exc:
                    await reconcile_uncertain(event, str(exc))
                processed = True
            if processed:
                continue
            due = self.store.handoff_next_due()
            timeout = max(0.1, due - time.time()) if due is not None else None
            self.handoff_changed.clear()
            try:
                await asyncio.wait_for(self.handoff_changed.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _public_task(item: dict) -> dict:
        result = {key: item.get(key) for key in (
            "id", "project_id", "status", "round_no", "created_at", "updated_at",
            "session_id", "exit_code", "exit_reason", "usage", "result",
            "snapshot_path", "review_note")}
        bundle = item.get("bundle")
        result["request"] = ({key: scrub(bundle.get(key)) for key in (
            "objective", "context", "acceptance", "deliverables", "scope",
            "limits", "question_policy", "actions")}) if isinstance(bundle, dict) else None
        result["handoff_mode"] = bundle.get("handoff", {}).get("mode", "manual") if isinstance(bundle, dict) else "manual"
        result["backend"] = bundle.get("backend", "read_only") if isinstance(bundle, dict) else None
        if isinstance(bundle, dict) and bundle.get("workspace"):
            result["workspace"] = bundle["workspace"]
        return result

    async def submit(self, params: dict) -> dict:
        request_id, existing = self._request_id(params, "submit_task")
        if existing:
            return existing
        project_id = safe_id(params.get("project_id"), "project_id")
        project = project_config(project_id)
        objective = self._text(params.get("objective"), "objective")
        acceptance = params.get("acceptance")
        deliverables = params.get("deliverables")
        context = params.get("context", "")
        question_policy = params.get("question_policy", "")
        scope = params.get("scope")
        parallel_ok = params.get("parallel_ok", False)
        if type(parallel_ok) is not bool:
            raise BridgeError("INVALID_ARGUMENT", "parallel_ok must be boolean")
        actions = params.get("actions", ["read"])
        limits = params.get("limits", {})
        if not isinstance(acceptance, list) or not acceptance or not all(isinstance(x, str) and x.strip() for x in acceptance):
            raise BridgeError("INVALID_ARGUMENT", "acceptance must be a nonempty list")
        if not isinstance(deliverables, list) or not deliverables or not all(isinstance(x, str) and x.strip() for x in deliverables):
            raise BridgeError("INVALID_ARGUMENT", "deliverables must be a nonempty list")
        if (not isinstance(context, str) or len(context) > 16000 or
                not isinstance(question_policy, str) or len(question_policy) > 4000):
            raise BridgeError("INVALID_ARGUMENT", "Invalid context or question_policy")
        if not isinstance(scope, list) or not scope or any(not isinstance(x, str) for x in scope):
            raise BridgeError("INVALID_ARGUMENT", "scope must list authorized paths")
        write_task = actions == ["read", "write", "execute"]
        if actions != ["read"] and not write_task:
            raise BridgeError("SANDBOX_UNAVAILABLE", "Only read or explicitly enabled read/write/execute tasks are supported")
        if write_task:
            if not project["write_backend"].get("enabled"):
                raise BridgeError("SANDBOX_UNAVAILABLE", "Native write backend is not enabled for this project")
            scope = validate_write_scope(project["write_backend"]["write_paths"], scope)
            project_ready(project)
        if not isinstance(limits, dict):
            raise BridgeError("INVALID_ARGUMENT", "limits must be an object")
        configured_max = self._number(project.get("limits", {}).get("seconds", 3600), 1, 86400)
        max_seconds = self._number(limits.get("seconds", configured_max), 1, 86400)
        if max_seconds > configured_max:
            raise BridgeError("LIMIT_REACHED", "Task time exceeds project limit")
        if "usd" in limits:
            raise BridgeError("INVALID_ARGUMENT", "CC cost is not a task limit; omit limits.usd")
        max_rounds = self._number(project.get("limits", {}).get("rounds", 3), 1, 10)
        requested_handoff = params.get("handoff", "manual")
        if not isinstance(requested_handoff, str) or requested_handoff not in {"manual", "automatic"}:
            raise BridgeError("INVALID_ARGUMENT", "handoff must be manual or automatic")
        binding = {"mode": "manual"}
        if requested_handoff == "automatic":
            configured = project.get("handoff")
            if not isinstance(configured, dict) or configured.get("mode") != "automatic":
                raise BridgeError("HANDOFF_UNAVAILABLE", "Automatic handoff is not configured for this project")
            from .handoff_host import AppServerHost
            binding = configured.copy()
            binding["project_root"] = project["root"]
            capability = await AppServerHost().probe(binding)
            if not capability.connected:
                raise BridgeError("HANDOFF_UNAVAILABLE", capability.reason)
        self._admit_parallel(project_id, project, {"scope": scope, "parallel_ok": parallel_ok})
        task_id = uuid.uuid4().hex
        workspace = None
        if write_task:
            workspace = create_worktree(project["root"], task_id, project["shared_context"])
            snapshot_path = Path(workspace["worktree_path"])
            digest = workspace["shared_context_sha256"]
            snapshot_info = {"base_commit": workspace["base_commit"],
                             "source_dirty": workspace["source_dirty"]}
        else:
            snapshot_parent = STATE / "snapshots"
            private_dir(snapshot_parent)
            snapshot_path = snapshot_parent / task_id
            digest, snapshot_info = snapshot(project, scope, snapshot_path)
        bundle = {"objective": objective, "acceptance": acceptance, "deliverables": deliverables,
                  "context": context, "scope": scope, "actions": actions,
                  "parallel_ok": parallel_ok,
                  "task_id": task_id,
                  "backend": "native_write" if write_task else "read_only", "workspace": workspace,
                  "limits": {"seconds": max_seconds, "rounds": max_rounds},
                  "question_policy": question_policy,
                  "shared_context": project["shared_context"], "snapshot_sha256": digest,
                  "snapshot_info": snapshot_info, "handoff": binding}
        try:
            self.store.create(task_id, project_id, request_id, bundle, project, str(snapshot_path))
        except sqlite3.IntegrityError as exc:
            if workspace:
                discard_unstarted(project["root"], workspace, task_id)
            else:
                shutil.rmtree(snapshot_path, ignore_errors=True)
            recovered = self.store.operation(request_id, "submit_task")
            if recovered:
                return recovered
            raise BridgeError("PROJECT_BUSY", "Another task is active in this project") from exc
        except Exception:
            if workspace:
                discard_unstarted(project["root"], workspace, task_id)
            else:
                shutil.rmtree(snapshot_path, ignore_errors=True)
            raise
        self.store.event(task_id, "queued", {"scope": scope, "snapshot_sha256": digest})
        result = {"task_id": task_id, "status": "queued", "backend": bundle["backend"],
                  "workspace": workspace,
                  "handoff": {"mode": binding["mode"],
                              "status": "connected" if binding["mode"] == "automatic" else "disabled"}}
        self.store.save_operation(request_id, "submit_task", result)
        self.jobs[task_id] = asyncio.create_task(self.run_task(task_id))
        return result

    async def continue_task(self, params: dict) -> dict:
        request_id, existing = self._request_id(params, "continue_task")
        if existing:
            return existing
        item = self.store.one(safe_id(params.get("task_id"), "task_id"))
        fresh_session = params.get("fresh_session", False)
        if type(fresh_session) is not bool:
            raise BridgeError("INVALID_ARGUMENT", "fresh_session must be boolean")
        resumable = item["status"] == "review_required" or (
            item["status"] == "interrupted" and item["bundle"].get("backend") == "native_write"
            and item["exit_reason"] == "daemon_restart")
        if not resumable or not item["session_id"]:
            raise BridgeError("INVALID_STATE", "Task cannot be resumed")
        if fresh_session and (item["status"] != "review_required" or
                              item["bundle"].get("backend") != "native_write"):
            raise BridgeError("INVALID_STATE", "Fresh-session relay requires a reviewed write task")
        if item["bundle"].get("backend") == "native_write":
            verify_worktree(item["project"]["root"], item["bundle"]["workspace"])
        if item["round_no"] >= item["bundle"]["limits"]["rounds"]:
            raise BridgeError("LIMIT_REACHED", "Task round limit reached")
        instruction = self._text(params.get("instruction"), "instruction")
        self._admit_parallel(item["project_id"], item["project"], item["bundle"], item["id"])
        round_no = item["round_no"] + 1
        self.store.transition(item["id"], "continuing", {"round_no": round_no},
                              before=("INSERT INTO rounds(task_id,round_no,instruction,status,request_id) VALUES(?,?,?,?,?)",
                                      (item["id"], round_no, instruction, "queued", request_id)),
                              status="continuing", round_no=round_no, result_json=None)
        result = {"task_id": item["id"], "round_no": round_no, "status": "continuing",
                  "fresh_session": fresh_session}
        self.store.save_operation(request_id, "continue_task", result)
        self.jobs[item["id"]] = asyncio.create_task(
            self.run_task(item["id"], instruction, fresh_session=fresh_session))
        return result

    def complete(self, params: dict) -> dict:
        request_id, existing = self._request_id(params, "complete_task")
        if existing:
            return existing
        item = self.store.one(safe_id(params.get("task_id"), "task_id"))
        failed_write = (item["status"] == "failed" and
                        (item.get("bundle") or {}).get("backend") == "native_write")
        if item["status"] != "review_required" and not failed_write:
            raise BridgeError("INVALID_STATE", "Only reviewed tasks or inspected failed write tasks can be completed")
        note = self._text(params.get("review_note"), "review_note")
        if failed_write:
            verify_worktree(item["project"]["root"], item["bundle"]["workspace"])
        self.store.transition(item["id"], "completed", {"review_note": note},
                              status="completed", review_note=note,
                              complete_request_id=request_id)
        result = {"task_id": item["id"], "status": "completed"}
        self.store.save_operation(request_id, "complete_task", result)
        return result

    def respond(self, params: dict) -> dict:
        request_id, existing = self._request_id(params, "respond_task")
        if existing:
            return existing
        task_id = safe_id(params.get("task_id"), "task_id")
        question_id = safe_id(params.get("question_id"), "question_id")
        answer = self._text(params.get("answer"), "answer")
        row = self.store.db.execute(
            "SELECT status FROM questions WHERE id=? AND task_id=?", (question_id, task_id)).fetchone()
        if not row:
            raise BridgeError("TASK_NOT_FOUND", "Question does not exist")
        if row["status"] != "pending" or self.store.one(task_id)["status"] != "waiting_answer":
            raise BridgeError("QUESTION_EXPIRED", "Question is no longer pending")
        self.store.transition(task_id, "answer", {"question_id": question_id},
                              before=("UPDATE questions SET status='answered',answer=?,request_id=?,answered_at=? WHERE id=?",
                                      (answer, request_id, time.time(), question_id)),
                              status="running")
        future = self.answers.pop(question_id, None)
        if future and not future.done():
            future.set_result(answer)
        result = {"task_id": task_id, "question_id": question_id, "status": "accepted"}
        self.store.save_operation(request_id, "respond_task", result)
        return result

    async def ask(self, params: dict) -> dict:
        task_id = safe_id(params.get("task_id"), "task_id")
        text = self._text(params.get("text"), "text", 4000)
        options = params.get("options", [])
        if not isinstance(options, list) or len(options) > 8 or any(not isinstance(x, str) for x in options):
            raise BridgeError("INVALID_ARGUMENT", "Invalid question options")
        item = self.store.one(task_id)
        if item["status"] != "running":
            raise BridgeError("INVALID_STATE", "Task is not running")
        question_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.answers[question_id] = future
        self.store.transition(task_id, "question", {"question_id": question_id, "text": text, "options": options},
                              question=(question_id, task_id, item["round_no"], text,
                                        json.dumps(options), time.time()), status="waiting_answer")
        self.handoff_changed.set()
        try:
            return {"question_id": question_id, "answer": await asyncio.wait_for(future, 24 * 3600)}
        except asyncio.TimeoutError as exc:
            self.store.db.execute("UPDATE questions SET status='expired' WHERE id=?", (question_id,))
            self.store.db.commit()
            self.store.transition(task_id, "question_expired", {"question_id": question_id},
                                  status="review_required", exit_reason="answer_timeout")
            self.handoff_changed.set()
            await self._stop_process(task_id)
            raise BridgeError("QUESTION_EXPIRED", "Question timed out") from exc
        finally:
            self.answers.pop(question_id, None)

    async def cancel(self, params: dict) -> dict:
        request_id, existing = self._request_id(params, "cancel_task")
        if existing:
            return existing
        item = self.store.one(safe_id(params.get("task_id"), "task_id"))
        if item["status"] not in {"queued", "running", "waiting_answer", "continuing"}:
            raise BridgeError("INVALID_STATE", "Task is not active")
        self.store.transition(item["id"], "canceled", {}, status="canceled",
                              exit_reason="user_cancel", cancel_request_id=request_id)
        await self._stop_process(item["id"])
        if (item.get("bundle") or {}).get("backend") == "native_write":
            try:
                workspace = artifacts(item["project"]["root"], item["bundle"]["workspace"],
                                      item["bundle"]["scope"])
            except BridgeError:
                workspace = item["bundle"]["workspace"]
            self.store.update(item["id"], result_json=json.dumps(scrub({"workspace": workspace}),
                                                            ensure_ascii=False))
            self.store.event(item["id"], "workspace_preserved", {"workspace": workspace})
        result = {"task_id": item["id"], "status": "canceled", "snapshot_path": item["snapshot_path"]}
        self.store.save_operation(request_id, "cancel_task", result)
        return result

    async def _stop_process(self, task_id: str) -> None:
        proc = self.processes.get(task_id)
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()

    async def run_task(self, task_id: str, instruction: str | None = None,
                       *, fresh_session: bool = False) -> None:
        item = self.store.one(task_id)
        bundle, project = item["bundle"], item["project"]
        write_task = bundle.get("backend") == "native_write"
        if item["status"] == "canceled":
            self.jobs.pop(task_id, None)
            return
        cli = project.get("claude_path") or shutil.which("claude")
        if not cli or not Path(cli).exists():
            self._fail(task_id, "CLI_FAILED", "Claude CLI is unavailable")
            return
        prompt = (self._fresh_prompt(bundle, instruction) if fresh_session
                  else instruction or self._prompt(bundle))
        mcp_dir = STATE / "mcp"
        private_dir(mcp_dir)
        mcp_config = mcp_dir / f"{task_id}.json"
        atomic_json(mcp_config, {"mcpServers": {"codex1cc_questions": {
            "command": str(Path(sys.executable).resolve()),
            "args": ["-m", "codex1cc.question_server"]}}})
        prior_cost = (item["usage"] or {}).get("total_cost_usd", 0)
        command = [cli, "-p", "--output-format", "stream-json", "--verbose"]
        if write_task:
            command += ["--permission-mode", "bypassPermissions", "--strict-mcp-config",
                        "--mcp-config", str(mcp_config), "--tools", "Read,Glob,Grep,Edit,Write,Bash"]
        else:
            command += ["--restricted", "--permission-mode", "dontAsk",
                        "--strict-mcp-config", "--mcp-config", str(mcp_config),
                        "--allowedTools", "Read", "Glob", "Grep", "mcp__codex1cc_questions__ask_codex",
                        "--tools", "Read,Glob,Grep"]
        if project.get("model"):
            command += ["--model", project["model"]]
        if item["session_id"] and not fresh_session:
            command += ["--resume", item["session_id"]]
        command.append(prompt)
        allowed_env = {"PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "CODEX1CC_STATE_DIR",
                       "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                       "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"}
        if write_task:
            allowed_env |= {"XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "DISPLAY",
                            "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS"}
        env = {key: value for key, value in os.environ.items() if key in allowed_env}
        try:
            env.update(provider_environment())
        except BridgeError as exc:
            self._fail(task_id, exc.code, str(exc))
            return
        env["CODEX1CC_TASK_ID"] = task_id
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        context_policy = project.get("context_policy") or {
            "auto_compact_window": 500000, "auto_compact_percent": 70}
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(context_policy["auto_compact_window"])
        env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(context_policy["auto_compact_percent"])
        if env.get("CLAUDE_CONFIG_DIR"):
            env["CLAUDE_CONFIG_DIR"] = str(Path(env["CLAUDE_CONFIG_DIR"]).expanduser().resolve())
        try:
            command[0] = str(Path(cli).resolve(strict=True))
            if write_task:
                verify_worktree(project["root"], bundle["workspace"])
            else:
                command = wrap_linux(cli, command, Path(item["snapshot_path"]), mcp_config)
        except BridgeError as exc:
            self._fail(task_id, exc.code, str(exc))
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, cwd=item["snapshot_path"], env=env, start_new_session=True,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024 + 1024)
            stderr_task = asyncio.create_task(self._collect_stderr(proc.stderr))
            self.processes[task_id] = proc
            if self.store.one(task_id)["status"] == "canceled":
                await self._stop_process(task_id)
                await stderr_task
                return
            self.store.update(task_id, status="running", round_no=max(1, item["round_no"]),
                              process_id=proc.pid)
            self.store.event(task_id, "running", {"round_no": max(1, item["round_no"])})
            active_elapsed = 0.0
            last_tick = time.monotonic()
            result = None
            last_error = None
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=1)
                except asyncio.TimeoutError:
                    line = None
                now = time.monotonic()
                if self.store.one(task_id)["status"] != "waiting_answer":
                    active_elapsed += now - last_tick
                last_tick = now
                if active_elapsed >= bundle["limits"]["seconds"]:
                    self._fail(task_id, "LIMIT_REACHED", "Task time limit reached")
                    await self._stop_process(task_id)
                    return
                if line is None:
                    continue
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    self.store.event(task_id, "output_truncated", {})
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    self.store.event(task_id, "malformed_output", {"preview": line[:1000].decode(errors="replace")})
                    continue
                if not isinstance(event, dict):
                    self.store.event(task_id, "malformed_output", {"preview": str(event)[:1000]})
                    continue
                if event.get("session_id"):
                    self.store.update(task_id, session_id=event["session_id"])
                if event.get("error"):
                    content = event.get("message", {}).get("content", []) if isinstance(event.get("message"), dict) else []
                    detail = " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
                    last_error = f"{event['error']}: {detail[:500]}".strip()
                if event.get("type") == "result":
                    result = event
                    self.store.event(task_id, "claude_result", event)
                else:
                    self.store.event(task_id, "claude_event", event)
            stderr = await stderr_task
            code = await proc.wait()
            if self.store.one(task_id)["status"] in {"canceled", "review_required"}:
                return
            if code or not result or result.get("is_error"):
                detail = " ".join(filter(None, (stderr, last_error,
                                                  str(result.get("result", "")) if result else "")))
                context_limit = self._context_limit_error(detail)
                self._fail(task_id, "CONTEXT_LIMIT" if context_limit else "CLI_FAILED",
                           detail or "Claude did not return a successful result")
                return
            summary = self._summary(result)
            if write_task:
                try:
                    summary["workspace"] = artifacts(project["root"], bundle["workspace"], bundle["scope"])
                except BridgeError as exc:
                    self._fail(task_id, exc.code, str(exc))
                    return
            round_cost = result.get("total_cost_usd")
            total_cost = (prior_cost + round_cost if isinstance(prior_cost, (int, float))
                          and isinstance(round_cost, (int, float)) else None)
            usage = {"total_cost_usd": total_cost, "last_round": result.get("usage")}
            self.store.transition(task_id, "review_required", summary, status="review_required", exit_code=code,
                                  result_json=json.dumps(scrub(summary), ensure_ascii=False),
                                  usage_json=json.dumps(scrub(usage), ensure_ascii=False))
            self.handoff_changed.set()
        except Exception as exc:
            self._fail(task_id, "CLI_FAILED", str(exc))
            await self._stop_process(task_id)
        finally:
            if self.store.one(task_id)["process_id"]:
                self.store.update(task_id, process_id=None)
            self.processes.pop(task_id, None)
            self.jobs.pop(task_id, None)

    @staticmethod
    def _prompt(bundle: dict) -> str:
        if bundle.get("backend") == "native_write":
            return (
                f"Codex1CC 任务 {bundle.get('task_id', '')}。目标：{bundle['objective']}\n"
                f"任务上下文：{bundle['context']}\n"
                f"固定共享信息文件：{bundle['workspace']['shared_context_snapshot']}\n"
                f"声明的改动范围：{json.dumps(bundle['scope'], ensure_ascii=False)}。"
                "仅在独立任务分支中工作；可运行测试和创建本地提交。"
                "不要推送、合并、部署或清理其他工作树。"
                f"验收标准：{json.dumps(bundle['acceptance'], ensure_ascii=False)}\n"
                f"交付物：{json.dumps(bundle['deliverables'], ensure_ascii=False)}\n"
                f"提问规则：{bundle['question_policy']}\n"
                "不要为 CC 费用或预算提问；只提真正阻塞目标的产品或授权问题。"
                "最终交付简短结论、逐项验收结果、提交与测试证据及未解决阻塞。"
            )
        return (
            f"目标：{bundle['objective']}\n任务上下文：{bundle['context']}\n"
            f"共享文件：{bundle['shared_context']}\n验收标准：{json.dumps(bundle['acceptance'], ensure_ascii=False)}\n"
            f"交付物：{json.dumps(bundle['deliverables'], ensure_ascii=False)}\n"
            f"提问规则：{bundle['question_policy']}\n"
            "不要为 CC 费用或预算提问；只提真正阻塞目标的产品或授权问题。"
            "只读取当前工作目录中的文件。最终只交付结论、逐项验收结果、产物路径和阻塞项；"
            "每项最多一句，总计尽量不超过 300 字。"
        )

    @classmethod
    def _fresh_prompt(cls, bundle: dict, instruction: str | None) -> str:
        return (cls._prompt(bundle) + "\n本轮在同一任务工作树中使用全新 CC 会话。"
                "先核对当前分支、已有提交、未提交改动和验收证据；不要重复已完成的工作。"
                f"本轮明确指令：{instruction or ''}\n"
                "门禁日志保留在文件中，只回报结论、退出码与路径。")

    @staticmethod
    def _summary(result: dict) -> dict:
        value = result.get("result", "")
        return {"conclusion": str(value)[:2000], "truncated": len(str(value)) > 2000,
                "subtype": result.get("subtype"),
                "session_id": result.get("session_id"),
                "cost_usd": result.get("total_cost_usd")}

    @staticmethod
    def _context_limit_error(detail: str) -> bool:
        lower = detail.lower()
        return any(marker in lower for marker in (
            "maximum context length", "context window exceeded", "context_length_exceeded",
            "prompt is too long", "too many tokens in prompt"))

    @staticmethod
    async def _collect_stderr(stream: asyncio.StreamReader) -> str:
        chunks = []
        size = 0
        while part := await stream.read(4096):
            if size < 16384:
                chunks.append(part[:16384 - size])
                size += len(chunks[-1])
        return b"".join(chunks).decode(errors="replace")

    def _fail(self, task_id: str, code: str, message: str) -> None:
        self.jobs.pop(task_id, None)
        item = self.store.one(task_id)
        if item["status"] in {"canceled", "review_required", "completed"}:
            return
        safe_message = scrub(message[:2000])
        summary = {"conclusion": safe_message}
        if code == "CONTEXT_LIMIT":
            summary["next_action"] = (
                "Review the saved work and usage before starting a fresh Claude session. "
                "Do not resume or retry the overflowing session automatically.")
        bundle = item.get("bundle") or {}
        if bundle.get("backend") == "native_write":
            try:
                summary["workspace"] = artifacts(item["project"]["root"],
                                                  bundle["workspace"], bundle["scope"])
            except BridgeError:
                summary["workspace"] = bundle["workspace"]
        self.store.transition(task_id, "failed", {"code": code, "message": safe_message,
                                                   "workspace": summary.get("workspace")},
                              status="failed", exit_reason=code,
                              result_json=json.dumps(scrub(summary), ensure_ascii=False))
        self.handoff_changed.set()

    def doctor(self) -> dict:
        ready, reason = linux_available()
        write_projects = {}
        parallel_projects = {}
        try:
            for project_id in projects():
                try:
                    project = project_config(project_id)
                    parallel_projects[project_id] = project["parallel"]["max_agents"]
                    if project["write_backend"].get("enabled"):
                        project_ready(project)
                        write_projects[project_id] = {"enabled": True, "ready": True}
                    else:
                        write_projects[project_id] = {"enabled": False, "ready": False}
                except BridgeError as exc:
                    write_projects[project_id] = {"enabled": True, "ready": False,
                                                  "reason": exc.code}
        except BridgeError:
            pass
        return {"protocol_version": 2, "platform": sys.platform, "claude_path": shutil.which("claude"),
                "config_path": str(__import__("codex1cc.common", fromlist=["CONFIG"]).CONFIG),
                "state_path": str(STATE), "sandbox_ready": ready, "reason": reason,
                "mode": "read_only" if ready else "disabled",
                "native_write": {"available": bool(shutil.which("git") and shutil.which("claude")),
                                 "trust_required": True, "projects": write_projects},
                "parallel": {"projects": parallel_projects}}


async def serve() -> None:
    os.umask(0o077)
    private_dir(STATE)
    if SOCKET.exists():
        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET))
            writer.close()
            await writer.wait_closed()
            raise RuntimeError("Executor is already running")
        except (ConnectionRefusedError, FileNotFoundError):
            SOCKET.unlink()
    shutdown_event = asyncio.Event()
    executor = Executor(shutdown_event)
    handoff_job = asyncio.create_task(executor.handoff_loop())
    for queued in executor.store.queued():
        executor.jobs[queued["id"]] = asyncio.create_task(executor.run_task(queued["id"]))

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw or len(raw) > MAX_LINE:
                raise BridgeError("INVALID_ARGUMENT", "Request is empty or too large")
            request = json.loads(raw)
            if not isinstance(request, dict) or not isinstance(request.get("params", {}), dict):
                raise BridgeError("INVALID_ARGUMENT", "Invalid request")
            data = await executor.dispatch(request.get("method"), request.get("params", {}))
            response = {"ok": True, "data": data}
        except BridgeError as exc:
            response = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            response = {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": str(exc)}}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(client, str(SOCKET))
    os.chmod(SOCKET, 0o600)
    async with server:
        await shutdown_event.wait()
    executor.stopping = True
    handoff_job.cancel()
    try:
        await handoff_job
    except asyncio.CancelledError:
        pass
    SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(serve())
