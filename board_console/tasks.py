"""Background workflow orchestration: one submission, no model round trip per step."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path

from pydantic import ValidationError

from .deploy import artifact, upload
from .execution import ExecutionError, checkpoint, evaluate, run_shell, shell_wire
from .manager import Manager, needs_input
from .task_models import CommandStep, Plan, ReadyStep, UploadStep

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Job:
    def __init__(self, folder: Path, plan: Plan, session, digest: str):
        self.folder, self.plan, self.session = folder, plan, session
        self.cancel = threading.Event()
        self.condition = threading.Condition(threading.RLock())
        self.thread = None
        self.snapshot = {"task_id": folder.name, "state": "queued", "revision": 0,
                         "plan_sha256": digest, "name": plan.name, "change_summary": plan.change_summary,
                         "session_id": session.id, "capture_id": session.id,
                         "target": session.capture.info()["connection"], "steps": [],
                         "created_time_ns": time.time_ns(), "result_path": str(folder / "result.json"),
                         "needs_analysis": False, "retry_safe": False}
        save_json(folder / "plan.json", plan.model_dump(mode="json"))
        self.update("queued")

    def update(self, event: str, **values) -> None:
        with self.condition:
            self.snapshot.update(values)
            self.snapshot["revision"] += 1
            record = {"seq": self.snapshot["revision"], "time_ns": time.time_ns(), "event": event, **values}
            with (self.folder / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            save_json(self.folder / "result.json", self.snapshot)
            self.condition.notify_all()

    def read(self) -> dict:
        with self.condition:
            return copy.deepcopy(self.snapshot)


class Tasks:
    def __init__(self, manager: Manager):
        self.manager = manager
        self.root = manager.data_dir / "tasks"
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.closing = False

    def prepare(self, session_id: str | None, raw: dict | None) -> dict:
        missing, questions = [], []
        if not session_id:
            missing.append("session_id")
            questions.append("本次要在哪个已连接开发板执行？请先复用或建立已登录 POSIX Shell 的会话。")
        if raw is None:
            missing.append("plan")
            questions.append("请补充本次部署目标、程序重启方式和修改的验收条件；Agent 应先查项目配置并生成计划。")
        else:
            try:
                Plan.model_validate(raw)
            except ValidationError as exc:
                errors = exc.errors(include_url=False, include_context=False, include_input=False)
                if any(item["type"] != "missing" for item in errors):
                    return {"status": "invalid_plan", "errors": errors, "device_io": False}
                for item in errors:
                    field = ".".join(map(str, item["loc"]))
                    missing.append("plan." + field)
                    questions.append(f"请补充计划字段 {field}；优先从已提供信息和项目配置取得。")
        if missing:
            return needs_input(missing, questions, device_io=False)
        return self.validate(session_id, raw)[1]

    def validate(self, session_id: str, raw: dict) -> tuple[Plan, dict]:
        plan = Plan.model_validate(raw)
        session = self.manager.get(session_id)
        if session.kind not in ("serial", "ssh") or session.state != "open" or session.stop.is_set():
            raise ValueError("task requires an open serial or SSH session")
        if session.requires_reconnect:
            raise ValueError("previous transaction ended uncertainly; reconnect before starting another task")
        files = []
        for step in plan.steps:
            if isinstance(step, UploadStep):
                files.append(artifact(self.manager.workspace, step, session.kind))
            else:
                shell_wire(step.command, plan.newline, session.encoding, "0" * 32)
        return plan, {"status": "valid", "session_id": session.id, "artifacts": files,
                      "step_names": [step.name for step in plan.steps], "timeout_seconds": plan.timeout_seconds,
                      "assumptions": ["target is an already logged-in POSIX shell",
                                      "restart commands restart the application, not the board or connection"],
                      "side_effects": "upload steps replace target files; command steps execute exactly the supplied commands"}

    def start(self, session_id: str, raw: dict, request_id: str) -> dict:
        request_id = self._folder(request_id).name
        plan = Plan.model_validate(raw)
        digest = hashlib.sha256(json.dumps(plan.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self.closing:
                raise ValueError("task runner is shutting down")
            folder = self._folder(request_id)
            if folder.exists():
                existing = self.status(request_id)
                if existing["plan_sha256"] != digest:
                    raise ValueError("request_id already belongs to a different plan")
                if self.manager.get(session_id).capture.info()["connection"] != existing["target"]:
                    raise ValueError("request_id already belongs to a different target")
                return {**existing, "reused": True}
            if sum(job.read()["state"] not in TERMINAL for job in self.jobs.values()) >= 4:
                raise ValueError("at most four tasks can run concurrently")
            self.validate(session_id, raw)
            session = self.manager.get(session_id)
            if not session.operation_lock.acquire(blocking=False):
                return {"status": "busy", "message": "session is already running a command or task"}
            job = None
            try:
                if session.requires_reconnect or session.state != "open" or session.stop.is_set():
                    raise ValueError("session state changed; reconnect before starting a task")
                folder.mkdir()  # Exclusive request ID reservation, including across processes.
                job = Job(folder, plan, session, digest)
                self.jobs[request_id] = job
                session.task_id = request_id
                job.thread = threading.Thread(target=self._run, args=(job,), name=f"task-{request_id}", daemon=True)
                job.thread.start()
            except Exception as exc:
                session.task_id = None
                session.operation_lock.release()
                if job is not None:
                    with job.condition:
                        job.snapshot.update(state="failed", failure={"code": "start_failed", "message": str(exc)},
                                            needs_analysis=True)
                        job.condition.notify_all()
                    job.update("start_failed")
                raise
            return job.read()

    def _run(self, job: Job) -> None:
        started = time.monotonic()
        deadline = started + job.plan.timeout_seconds
        index = -1
        step_start = started
        try:
            job.update("started", state="running", started_time_ns=time.time_ns())
            for index, step in enumerate(job.plan.steps):
                step_start = time.monotonic()
                checkpoint(job.cancel, deadline)
                step_deadline = min(deadline, step_start + step.timeout_seconds)
                job.update("step_started", active_step=step.name, active_index=index,
                           phase=step.phase if isinstance(step, CommandStep) else step.kind)
                if isinstance(step, UploadStep):
                    result = upload(job.session, self.manager.workspace, step, job.plan.newline, job.cancel, step_deadline)
                    assertions = [{"check": "artifact_sha256", "expected": step.sha256,
                                   "actual": result["sha256"], "passed": result["remote_hash_verified"]}]
                elif isinstance(step, ReadyStep):
                    attempts = 0
                    result = {}
                    while True:
                        try:
                            checkpoint(job.cancel, step_deadline)
                        except ExecutionError as exc:
                            exc.evidence.update({"last_probe": result, "attempts": attempts,
                                                 "stdout_tail": result.get("stdout", "")[-8192:]})
                            raise
                        attempts += 1
                        result = run_shell(job.session, step.command, job.plan.newline, job.cancel,
                                           min(step_deadline, time.monotonic() + step.probe_timeout_seconds))
                        assertions = evaluate(result, step.checks)
                        if all(item["passed"] for item in assertions):
                            break
                        job.cancel.wait(min(step.interval_seconds, max(0, step_deadline - time.monotonic())))
                    result["attempts"] = attempts
                else:
                    result = run_shell(job.session, step.command, job.plan.newline, job.cancel, step_deadline)
                    assertions = evaluate(result, step.checks)
                result.update({"name": step.name, "assertions": assertions,
                               "step_duration_seconds": round(time.monotonic() - step_start, 4)})
                evidence_path = job.folder / f"step-{index + 1:02}.json"
                save_json(evidence_path, result)
                passed = all(item["passed"] for item in assertions)
                summary = {"name": step.name, "state": "passed" if passed else "failed",
                           "duration_seconds": result["step_duration_seconds"], "evidence_path": str(evidence_path),
                           "assertions": assertions}
                job.update("step_finished", steps=[*job.read()["steps"], summary])
                if not passed:
                    raise ExecutionError("assertion_failed", f"verification failed: {step.name}", result)
            checkpoint(job.cancel, deadline)
            job.update("finished", state="succeeded", active_step=None, active_index=None,
                       duration_seconds=round(time.monotonic() - started, 4), ended_time_ns=time.time_ns())
        except Exception as exc:
            if isinstance(exc, ExecutionError):
                code, evidence = exc.code, exc.evidence
            else:
                code, evidence = "execution_error", {}
            failure = {"code": code, "message": str(exc), "step_index": index,
                       "step_name": job.plan.steps[index].name if index >= 0 else None, "evidence": evidence,
                       "step_duration_seconds": round(time.monotonic() - step_start, 4) if index >= 0 else None}
            try:
                save_json(job.folder / "failure.json", failure)
                job.update("finished", state="cancelled" if code == "cancelled" else "failed",
                           failure={"code": code, "message": str(exc), "step_name": failure["step_name"],
                                    "step_duration_seconds": failure["step_duration_seconds"],
                                    "evidence_path": str(job.folder / "failure.json"),
                                    "stdout_tail": evidence.get("stdout", evidence.get("stdout_tail", ""))[-4096:]},
                           needs_analysis=code != "cancelled", ended_time_ns=time.time_ns(),
                           duration_seconds=round(time.monotonic() - started, 4))
            except Exception:
                # Preserve observable terminal state even if the evidence disk is full.
                with job.condition:
                    job.snapshot.update(state="failed", failure={"code": "evidence_write_failed", "message": str(exc)},
                                        needs_analysis=True)
                    job.condition.notify_all()
        finally:
            job.session.task_id = None
            job.session.operation_lock.release()

    def _folder(self, task_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", task_id):
            raise ValueError("request_id/task_id must be 1..64 ASCII letters, digits, underscores or hyphens")
        if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", task_id, re.IGNORECASE):
            raise ValueError("request_id/task_id cannot be a Windows reserved device name")
        return self.root / task_id.lower()

    def status(self, task_id: str) -> dict:
        folder = self._folder(task_id)
        with self.lock:
            if folder.name in self.jobs:
                return self.jobs[folder.name].read()
        result_path = folder / "result.json"
        if not result_path.exists():
            raise ValueError("task not found or its initial evidence was not persisted; do not resubmit with a new ID blindly")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result["state"] not in TERMINAL:
            result.update(state="interrupted", needs_analysis=True,
                          note="not owned by this MCP process; inspect device state before resubmitting; no automatic resume")
        return result

    def wait(self, task_id: str, timeout_seconds: float) -> dict:
        if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be 0..30")
        with self.lock:
            job = self.jobs.get(self._folder(task_id).name)
        if job is None:
            return self.status(task_id)
        deadline = time.monotonic() + timeout_seconds
        with job.condition:
            while job.snapshot["state"] not in TERMINAL and time.monotonic() < deadline:
                job.condition.wait(max(0, deadline - time.monotonic()))
            return job.read()

    def events(self, task_id: str, after: int, limit: int) -> dict:
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("after must be >= 0; limit must be 1..100")
        folder = self._folder(task_id)
        self.status(task_id)
        rows = []
        with (folder / "events.jsonl").open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                event = json.loads(line)
                if event["seq"] > after:
                    rows.append(event)
                if len(rows) > limit:
                    break
        selected = rows[:limit]
        return {"events": selected, "next_cursor": selected[-1]["seq"] if selected else after,
                "has_more": len(rows) > limit}

    def cancel(self, task_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(self._folder(task_id).name)
        if job is None:
            return self.status(task_id)
        with job.condition:
            if job.snapshot["state"] not in TERMINAL:
                job.cancel.set()
                job.update("cancel_requested", cancel_requested=True)
        return job.read()

    def close(self) -> None:
        with self.lock:
            self.closing = True
            jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel.set()
        deadline = time.monotonic() + 10
        for job in jobs:
            if job.thread and job.thread.ident is not None:
                job.thread.join(timeout=max(0, deadline - time.monotonic()))
