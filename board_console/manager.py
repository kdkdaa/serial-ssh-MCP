"""Session ownership and capture discovery."""

from __future__ import annotations

import codecs
import math
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .capture import Capture
from .sessions import Session
from .ssh_auth import HostTrust, SSHFailure, classify_failure, probe_host
from .transports import SSHTransport, SerialTransport, serial_ports


def needs_input(fields: list[str], questions: list[str], **context) -> dict:
    return {"status": "needs_input", "missing_fields": fields, "questions": questions,
            "next_action": "Ask the user these questions, reuse already supplied answers, then call again. Do not guess or connect.",
            **context}


class Manager:
    def __init__(self, workspace: Path, data_dir: Path):
        self.workspace = workspace.resolve()
        self.data_dir = data_dir.resolve()
        self.capture_dir = self.data_dir / "captures"
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.ssh_trust_store = self.data_dir / "ssh_known_hosts"

    def new(self, kind: str, metadata: dict, encoding: str) -> Session:
        codecs.lookup(encoding)
        if sum(s.state in ("open", "opening") for s in self.sessions.values()) >= 8:
            raise ValueError("at most 8 live sessions; close an existing session first")
        capture_id = uuid.uuid4().hex
        capture = Capture(self.capture_dir / f"{capture_id}.sqlite3", {
            "format_version": 1, "capture_id": capture_id, "kind": kind, "encoding": encoding,
            "state": "opening", "created_time_ns": time.time_ns(), "connection": metadata,
        })
        session = Session(capture, kind, encoding)
        self.sessions[capture_id] = session
        return session

    def get(self, session_id: str) -> Session:
        with self.lock:
            if session_id not in self.sessions:
                raise ValueError("unknown session_id; sessions do not survive MCP restart, captures do")
            return self.sessions[session_id]

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
            for existing in self.sessions.values():
                info = existing.capture.info()
                if existing.kind == "serial" and existing.state in ("open", "opening") and info["connection"]["port"].casefold() == port.casefold():
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
        path = self.capture_dir / f"{capture_id}.sqlite3"
        if not path.is_file():
            raise ValueError("capture not found")
        return path

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

    def captures(self, offset: int, limit: int) -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset must be >= 0; limit must be 1..100")
        paths = sorted(self.capture_dir.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        items = []
        for path in paths[offset:offset + limit]:
            try:
                with self.capture(path.stem) as capture:
                    info = capture.info()
                    if path.stem not in self.sessions and info.get("state") in ("open", "opening"):
                        info["state"] = "interrupted"
                        info["note"] = "No live session in this server; capture may come from an interrupted process"
                    items.append(info)
            except Exception as exc:
                items.append({"capture_id": path.stem, "error": str(exc)})
        return {"captures": items, "next_offset": offset + len(items), "has_more": offset + len(items) < len(paths)}

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
