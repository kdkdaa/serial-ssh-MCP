"""Session ownership and capture discovery."""

from __future__ import annotations

import codecs
import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .capture import Capture
from .sessions import Session
from .process_transport import ProcessTransport
from .ssh_auth import HostTrust, SSHFailure, classify_failure, probe_host
from .transports import SSHTransport, SerialTransport, serial_ports


def needs_input(fields: list[str], questions: list[str], **context) -> dict:
    return {"status": "needs_input", "missing_fields": fields, "questions": questions,
            "next_action": "Ask the user these questions, reuse already supplied answers, then call again. Do not guess or connect.",
            **context}


class Manager:
    def __init__(self, workspace: Path, data_dir: Path):
        self.workspace = workspace.resolve()
        self.project_data_dir = data_dir.resolve()
        self.instance_id = uuid.uuid4().hex
        self.data_dir = self.project_data_dir / "instances" / self.instance_id
        self.capture_dir = self.data_dir / "captures"
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "instance.json").write_text(json.dumps({
            "instance_id": self.instance_id, "pid": os.getpid(), "workspace": str(self.workspace),
            "created_time_ns": time.time_ns(), "lifecycle": "owned by the Agent stdio connection",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        # Keep explicitly confirmed trust across launches; never share live sessions.
        self.ssh_trust_store = self.project_data_dir / "ssh_known_hosts"

    def new(self, kind: str, metadata: dict, encoding: str) -> Session:
        codecs.lookup(encoding)
        active = lambda s: s.state in ("open", "opening") or s.cleanup_error or (s.thread and s.thread.is_alive())
        ended = [s for s in self.sessions.values() if not active(s) and s.task_id is None]
        for previous in ended[:-127]:
            previous.capture.close()
            del self.sessions[previous.id]
        if sum(bool(active(s)) for s in self.sessions.values()) >= 8:
            raise ValueError("at most 8 live sessions; close an existing session first")
        capture_id = uuid.uuid4().hex
        capture = Capture(self.capture_dir / f"{capture_id}.sqlite3", {
            "format_version": 1, "capture_id": capture_id, "kind": kind, "encoding": encoding,
            "state": "opening", "created_time_ns": time.time_ns(), "connection": metadata,
            "instance_id": self.instance_id, "workspace": str(self.workspace),
        })
        session = Session(capture, kind, encoding)
        self.sessions[capture_id] = session
        return session

    def get(self, session_id: str) -> Session:
        with self.lock:
            if session_id not in self.sessions:
                raise ValueError("unknown session_id; sessions do not survive MCP restart, captures do")
            return self.sessions[session_id]

    def port_owner(self, port: str) -> Session | None:
        for session in self.sessions.values():
            target = session.connection.get("port") if session.kind == "serial" else session.connection.get("resource_port")
            if (target and target.casefold() == port.casefold() and
                    (session.state in ("open", "opening") or session.cleanup_error or
                     (session.thread and session.thread.is_alive()))):
                return session
        return None

    def process_open(self, argv: list[str], cwd: str | None, env: dict[str, str | None],
                     encoding: str, resource_port: str | None, input_target: str,
                     required_files: list[str]) -> dict:
        directory = (self.workspace / cwd).resolve() if cwd else self.workspace
        absent = [str(directory / path) for path in required_files if not (directory / path).is_file()]
        if absent:
            return {"status": "missing_prerequisites", "missing_files": absent, "started": False}
        with self.lock:
            owner = self.port_owner(resource_port) if resource_port else None
            if owner:
                return {"status": "resource_busy", "session_id": owner.id, "resource_port": resource_port,
                        "next_action": "Explicitly close the owning session before starting another tool on this port."}
            metadata = {"argv": argv, "cwd": str(directory), "environment_keys": sorted(env),
                        "resource_port": resource_port, "input_target": input_target,
                        "required_files": required_files}
            session = self.new("process", metadata, encoding)
            transport = None
            try:
                transport = ProcessTransport(argv, directory, env)
                session.start(transport)
            except Exception as exc:
                if transport:
                    transport.close()
                session._finish("error", str(exc))
                return {"status": "start_failed", **session.status()}
        return {"status": "started", **session.status(),
                "next_action": "Read startup output with session_exchange; process creation is not tool/device readiness.",
                "note": "Input goes unchanged to tool stdin. Pipes are not a PTY; tool syntax/buffering may differ."}

    def serial_reconnect(self, session_id: str, port: str | None, timeout_seconds: float) -> dict:
        if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 15:
            raise ValueError("timeout_seconds must be 0..15")
        old = self.get(session_id)
        if old.kind != "serial":
            raise ValueError("serial_reconnect requires a serial session; reopen other transports explicitly")
        settings = old.connection
        identity = settings.get("usb") or {}
        identifiable = bool(identity.get("serial_number") and identity.get("vid") is not None and identity.get("pid") is not None)
        if not identifiable and not port:
            return needs_input(["port"], ["原连接没有唯一 USB 序列号，请确认重连端口。"], candidates=serial_ports())
        if not old.operation_lock.acquire(blocking=False):
            return {"status": "busy", "session_id": old.id}
        try:
            closed = old.close()
            if closed["status"] != "closed":
                return closed
            deadline = time.monotonic() + timeout_seconds
            while True:
                candidates = serial_ports()
                matches = [p for p in candidates if
                           (not port or p["port"].casefold() == port.casefold()) and
                           (not identifiable or all(p.get(k) == identity.get(k) for k in ("vid", "pid", "serial_number")))]
                if len(matches) == 1:
                    chosen = matches[0]["port"]
                    result = self.serial_open(chosen, settings["baudrate"], settings["bytesize"],
                                              settings["parity"], settings["stopbits"], old.encoding,
                                              settings["dtr"], settings["rts"], settings["rtscts"])
                    return {**result, "previous_session_id": old.id, "usb_identity_verified": identifiable,
                            "resent_bytes": 0}
                if len(matches) > 1:
                    return needs_input(["port"], ["同一 USB 身份匹配多个接口，请明确端口。"], candidates=matches)
                if time.monotonic() >= deadline:
                    return {"status": "device_not_found", "previous_session_id": old.id,
                            "expected_usb": identity, "candidates": candidates, "resent_bytes": 0,
                            "next_action": "Check enumeration/identity, then call reconnect again; no write was retried."}
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        finally:
            old.operation_lock.release()

    def serial_open(self, port: str | None, baudrate: int | None, bytesize: int, parity: str,
                    stopbits: float, encoding: str, dtr: bool, rts: bool, rtscts: bool) -> dict:
        missing, questions = [], []
        if not port:
            missing.append("port")
            questions.append("要连接哪个串口或哪块开发板？请从 candidates 中确认目标；没有候选时先连接设备。")
        if baudrate is None:
            missing.append("baudrate")
            questions.append("开发板的串口波特率是多少？不知道时可提供型号或项目路径，以便查阅配置。")
        if missing:
            return needs_input(missing, questions, candidates=serial_ports(),
                               defaults={"bytesize": bytesize, "parity": parity, "stopbits": stopbits,
                                         "dtr": dtr, "rts": rts, "encoding": encoding})
        with self.lock:
            owner = self.port_owner(port)
            if owner and (owner.kind != "serial" or owner.stop.is_set()):
                return {"status": "resource_busy", "session_id": owner.id,
                        "next_action": "Close the owning session and wait for release before opening the port."}
            for existing in self.sessions.values():
                info = existing.capture.info()
                if existing.kind == "serial" and existing.state in ("open", "opening") and info["connection"]["port"].casefold() == port.casefold():
                    requested = {"baudrate": baudrate, "bytesize": bytesize, "parity": parity,
                                 "stopbits": stopbits, "dtr": dtr, "rts": rts, "rtscts": rtscts}
                    if existing.encoding != encoding or any(info["connection"].get(k) != v for k, v in requested.items()):
                        return {"status": "session_conflict", **existing.status(), "connection": info["connection"],
                                "next_action": "Existing serial settings differ; explicitly close before changing settings."}
                    return {"status": "already_open", **existing.status(), "connection": info["connection"],
                            "next_action": "reuse this session, or explicitly close it before changing settings"}
            metadata = {"port": port, "baudrate": baudrate, "bytesize": bytesize, "parity": parity,
                        "stopbits": stopbits, "dtr": dtr, "rts": rts, "rtscts": rtscts,
                        "usb": next((p for p in serial_ports() if p["port"].casefold() == port.casefold()), None)}
            session = self.new("serial", metadata, encoding)
            transport = None
            try:
                transport = SerialTransport(port, baudrate, bytesize, parity, stopbits, dtr, rts, rtscts)
                session.start(transport)
            except Exception as exc:
                if transport:
                    transport.close()
                session._finish("error", str(exc))
                return {"status": "open_failed", **session.status()}
            return {"status": "opened", **session.status(), "connection": metadata,
                    "note": "DTR/RTS were set before open, but some USB drivers may still pulse them on open"}

    def ssh_open(self, host: str | None, username: str | None, port: int, key_file: str | None,
                 known_hosts: str | None, password_env: str | None, encoding: str,
                 password: str | None = None, host_key_sha256: str | None = None,
                 remember_host_key: bool = False, reuse: bool = True,
                 key_passphrase: str | None = None) -> dict:
        missing, questions = [], []
        if not host:
            missing.append("host")
            questions.append("要连接哪台 SSH 主机？请提供实际主机名或 IP。")
        if not username:
            missing.append("username")
            questions.append("SSH 登录用户名是什么？")
        if missing:
            return needs_input(missing, questions)
        with self.lock:
            if reuse:
                for existing in self.sessions.values():
                    info = existing.capture.info()
                    target = info["connection"]
                    if (existing.kind == "ssh" and existing.state in ("open", "opening") and
                            not existing.stop.is_set() and target["host"].casefold() == host.casefold() and
                            target["port"] == port and target["username"] == username):
                        if existing.state == "opening":
                            return {"status": "connecting", **existing.status(), "authentication_performed": False,
                                    "next_action": "An SSH connection is already being established; inspect session_list before retrying."}
                        identity = info.get("ssh_host_key", {})
                        if host_key_sha256 is not None and identity.get("host_key_sha256") != host_key_sha256:
                            return SSHFailure("host_key_mismatch", "The existing connection does not match the supplied fingerprint.",
                                              "host_key_mismatch", host_key=identity).result()
                        if existing.encoding != encoding:
                            return {"status": "session_conflict", "session_id": existing.id,
                                    "message": "Existing session uses a different encoding; reuse it or explicitly close/reopen."}
                        if remember_host_key:
                            try:
                                trust = HostTrust(host, port, self.ssh_trust_store, known_hosts,
                                                  host_key_sha256, remember=True)
                                key = existing.transport.client.get_transport().get_remote_server_key()
                                trust.missing_host_key(None, trust.hostname, key)
                                identity = trust.identity
                                existing.capture.set_info({"ssh_host_key": identity})
                            except Exception as exc:
                                return {**classify_failure(exc).result(), "session_id": existing.id,
                                        "authentication_performed": False}
                        return {"status": "already_open", **existing.status(), "host_key": identity,
                                "authentication_performed": False, "trust_saved_this_call": remember_host_key,
                                "next_action": "Use session_exchange on this session. Supplied credentials were not tried; use reuse=false for a fresh connection."}
            session = self.new("ssh", {"host": host, "port": port, "username": username}, encoding)
        transport = None
        started = time.monotonic()
        try:
            transport = SSHTransport(host, username, port, key_file, known_hosts, password_env,
                                     password=password, host_key_sha256=host_key_sha256,
                                     remember_host_key=remember_host_key, trust_store=self.ssh_trust_store,
                                     key_passphrase=key_passphrase)
            session.capture.set_info({"ssh_host_key": transport.host_key})
            session.start(transport)
        except Exception as exc:
            if transport:
                transport.close()
            failure = classify_failure(exc)
            session._finish("error", f"{failure.code}: {failure}")
            return {**session.status(), **failure.result(),
                    "duration_seconds": round(time.monotonic() - started, 4)}
        return {"status": "opened", **session.status(), "host_key": transport.host_key,
                "duration_seconds": round(time.monotonic() - started, 4),
                "note": "Authenticated persistent PTY; use session_exchange. stdout/stderr are merged."}

    def ssh_probe(self, host: str | None, port: int, known_hosts: str | None) -> dict:
        if not host:
            return needs_input(["host"], ["要检查哪台 SSH 主机的身份？请提供实际主机名或 IP。"])
        return probe_host(host, port, self.ssh_trust_store, known_hosts)

    def capture_path(self, capture_id: str) -> Path:
        if len(capture_id) != 32 or any(c not in "0123456789abcdef" for c in capture_id):
            raise ValueError("invalid capture_id; use capture_list")
        name = f"{capture_id}.sqlite3"
        paths = [self.capture_dir / name, self.project_data_dir / "captures" / name]
        paths.extend(self.project_data_dir.glob(f"instances/*/captures/{name}"))
        matches = {p.resolve() for p in paths if p.is_file() and p.resolve().is_relative_to(self.project_data_dir)}
        if not matches:
            raise ValueError("capture not found")
        if len(matches) != 1:
            raise ValueError("duplicate capture ID in project history")
        return matches.pop()

    @contextmanager
    def capture(self, capture_id: str):
        with self.lock:
            live = self.sessions.get(capture_id)
        if live:
            yield live.capture
        else:
            capture = Capture(self.capture_path(capture_id))
            try:
                yield capture
            finally:
                capture.close()

    def captures(self, offset: int, limit: int, scope: str = "instance") -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be >= 0; limit must be 1..100")
        if scope not in ("instance", "project"):
            raise ValueError("scope must be instance or project")
        paths = list(self.capture_dir.glob("*.sqlite3"))
        if scope == "project":
            paths += list(self.project_data_dir.glob("instances/*/captures/*.sqlite3"))
            paths += list((self.project_data_dir / "captures").glob("*.sqlite3"))
        paths = sorted({p.resolve() for p in paths if p.resolve().is_relative_to(self.project_data_dir)},
                       key=lambda p: p.stat().st_mtime_ns, reverse=True)
        items = []
        for path in paths[offset:offset + limit]:
            try:
                with self.capture(path.stem) as capture:
                    info = capture.info()
                    info["owned_by_this_instance"] = path.stem in self.sessions
                    if path.stem not in self.sessions and info.get("state") in ("open", "opening"):
                        info["recorded_state"] = info["state"]
                        info["state"] = "owner_unknown"
                        info["note"] = "Historical evidence only; this instance does not know whether its owner is alive."
                    items.append(info)
            except Exception as exc:
                items.append({"capture_id": path.stem, "error": str(exc)})
        return {"captures": items, "scope": scope, "instance_id": self.instance_id,
                "next_offset": offset + len(items), "has_more": offset + len(items) < len(paths)}

    def replay(self, capture_id: str, speed: float) -> dict:
        if not math.isfinite(speed) or not 0 <= speed <= 100:
            raise ValueError("speed must be 0..100")
        source_path = self.capture_path(capture_id)
        with self.capture(capture_id) as source:
            info = source.info()
        with self.lock:
            session = self.new("replay", {"source_capture_id": capture_id, "speed": speed,
                                          "source_last_cursor": info["last_cursor"]}, info["encoding"])
            session.start_replay(source_path, speed, info["last_cursor"])
        return {"status": "started", **session.status(), "mode": "offline",
                "note": "TX is historical evidence only; no bytes will be sent to any hardware"}

    def workspace_read(self, path: str, offset: int, limit: int) -> dict:
        if offset < 0 or not 1 <= limit <= 65536:
            raise ValueError("offset must be >= 0; limit must be 1..65536")
        source = (self.workspace / path).resolve()
        if not source.is_relative_to(self.workspace):
            raise ValueError("path is outside configured workspace; use host file tools for explicitly authorized external paths")
        with source.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(limit)
        return {"path": str(source), "offset": offset, "next_offset": offset + len(data),
                "has_more": offset + len(data) < source.stat().st_size,
                "text": data.decode("utf-8", "replace"), "hex": data.hex()}

    def close(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()
            if (not session.thread or not session.thread.is_alive()) and session.task_id is None:
                session.capture.close()
