"""Live readers, serialized writes and condition-based waits."""

from __future__ import annotations

import codecs
import logging
import math
import threading
import time
from pathlib import Path

from .capture import Capture
from .process_transport import ProcessExited

LOG = logging.getLogger(__name__)


class Session:
    def __init__(self, capture: Capture, kind: str, encoding: str = "utf-8"):
        self.capture = capture
        self.id = capture.path.stem
        self.kind = kind
        self.encoding = encoding
        self.state = "opening"
        self.error = None
        self.transport = None
        self.started_ns = time.monotonic_ns()
        self.condition = threading.Condition(threading.RLock())
        self.operation_lock = threading.Lock()
        self.write_lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.task_id = None
        self.requires_reconnect = False
        self.cleanup_error = None
        self.connection = capture.info().get("connection", {})
        self.byte_counts = {"rx": 0, "tx": 0}
        self.last_activity_ns = {"rx": None, "tx": None}
        self.decoders = {k: codecs.getincrementaldecoder(encoding)(errors="replace") for k in ("rx", "tx")}

    def record(self, kind: str, data: bytes = b"", details: dict | None = None, text: str | None = None) -> int:
        with self.condition:
            if text is None:
                text = self.decoders[kind].decode(data) if kind in self.decoders else ""
            seq = self.capture.append(kind, data, text, time.monotonic_ns() - self.started_ns, details or {})
            if kind in self.byte_counts and data:
                self.byte_counts[kind] += len(data)
                self.last_activity_ns[kind] = time.time_ns()
            self.condition.notify_all()
            return seq

    def start(self, transport) -> None:
        if self.stop.is_set():
            transport.close()
            raise RuntimeError("session was closed while connecting")
        self.transport = transport
        self.state = "open"
        self.capture.set_info({"state": self.state})
        self.record("opened")
        self.thread = threading.Thread(target=self._receive, name=f"board-{self.id}", daemon=True)
        self.thread.start()

    def _finish(self, state: str, error: str | None = None) -> None:
        with self.condition:
            self.state = state
            self.error = error
            try:
                for kind, decoder in self.decoders.items():
                    text = decoder.decode(b"", final=True)
                    if text:
                        self.record(kind, text=text)
                self.record(state, details={"error": error} if error else {})
            except Exception:
                LOG.exception("Unable to finalize capture %s", self.id)
            try:
                # Even a full event budget should leave an explicit terminal state.
                info = {"state": state, "error": error, "ended_time_ns": time.time_ns(),
                        "rx_bytes": self.byte_counts["rx"], "tx_bytes": self.byte_counts["tx"]}
                if self.kind == "process" and self.transport:
                    info["process"] = self.transport.status()
                self.capture.set_info(info)
            except Exception:
                LOG.exception("Unable to save terminal state %s", self.id)
            self.capture.release_writer()
            self.condition.notify_all()

    def _receive(self) -> None:
        state, error = "closed", None
        try:
            while not self.stop.is_set():
                data = self.transport.read()
                if data:
                    self.record("rx", data)
        except ProcessExited as exc:
            if not self.stop.is_set():
                state = "completed" if exc.returncode == 0 else "error"
                error = None if exc.returncode == 0 else str(exc)
                self.record("process_exit", details={"exit_code": exc.returncode})
                self.capture.set_info({"exit_code": exc.returncode})
        except Exception as exc:
            if not self.stop.is_set():
                state, error = "error", f"{type(exc).__name__}: {exc}"
                self.requires_reconnect = self.kind in ("serial", "ssh")
        finally:
            self.stop.set()
            with self.write_lock:
                try:
                    self.transport.close()
                except Exception as exc:
                    LOG.exception("Unable to close transport %s", self.id)
                    state, error = "error", f"transport cleanup failed: {exc}"
                    self.cleanup_error = str(exc)
                self._finish("error" if self.error else state, self.error or error)

    def start_replay(self, source_path: Path, speed: float, source_last_cursor: int) -> None:
        if not math.isfinite(speed) or not 0 <= speed <= 100:
            raise ValueError("speed must be 0..100; 0 means no timing delay")
        self.state = "open"
        self.capture.set_info({"state": "open"})
        self.record("opened", details={"replay": True, "source_last_cursor": source_last_cursor})

        def replay() -> None:
            source = None
            state, error = "completed", None
            try:
                source = Capture(source_path)
                cursor = 0
                baseline = time.monotonic()
                while cursor < source_last_cursor and not self.stop.is_set():
                    page = source.page(cursor)
                    for event in page["events"]:
                        if event["seq"] > source_last_cursor or self.stop.is_set():
                            break
                        if speed:
                            delay = event["elapsed_ns"] / 1e9 / speed - (time.monotonic() - baseline)
                            if self.stop.wait(max(0, delay)):
                                break
                        cursor = event["seq"]
                        self.record(event["kind"], bytes.fromhex(event["hex"]),
                                    {"source_seq": cursor, "source_time_ns": event["time_ns"],
                                     "source_elapsed_ns": event["elapsed_ns"], "source_details": event["details"]},
                                    text=event["text"])
                    if not page["events"]:
                        break
                if self.stop.is_set():
                    state = "closed"
            except Exception as exc:
                state, error = "error", f"{type(exc).__name__}: {exc}"
            finally:
                if source:
                    source.close()
                self.stop.set()
                self._finish(state, error)

        self.thread = threading.Thread(target=replay, name=f"replay-{self.id}", daemon=True)
        self.thread.start()

    def status(self) -> dict:
        with self.condition:
            return {"session_id": self.id, "capture_id": self.id, "kind": self.kind, "state": self.state,
                    "error": self.error, "last_cursor": self.capture.last_cursor(),
                    "capture_path": str(self.capture.path), "read_only": self.kind == "replay",
                    "task_id": self.task_id, "requires_reconnect": self.requires_reconnect,
                    "cleanup_error": self.cleanup_error,
                    "rx_bytes": self.byte_counts["rx"], "tx_bytes": self.byte_counts["tx"],
                    "last_rx_time_ns": self.last_activity_ns["rx"],
                    "last_tx_time_ns": self.last_activity_ns["tx"],
                    "output_source": "host_tool_stdout_stderr" if self.kind == "process" else "transport",
                    "input_target": self.connection.get("input_target", "device"),
                    "device_ready": "unknown",
                    "process": self.transport.status() if self.kind == "process" and self.transport else None}

    def read(self, after: int, limit: int, wait_seconds: float) -> dict:
        if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be 0..30")
        deadline = time.monotonic() + wait_seconds
        with self.condition:
            page = self.capture.page(after, limit)
            while not page["events"] and self.state == "open" and time.monotonic() < deadline:
                self.condition.wait(max(0, deadline - time.monotonic()))
                page = self.capture.page(after, limit)
            return {**self.status(), **page}

    def _send(self, data: bytes) -> dict:
        with self.write_lock:
            return self._send_locked(data)

    def _send_locked(self, data: bytes) -> dict:
        if not data or len(data) > 65536:
            raise ValueError("send length must be 1..65536 bytes")
        if self.state != "open" or self.stop.is_set() or self.kind == "replay":
            raise ValueError("session is not a writable live connection")
        # Record intent before any side effect; tx records only acknowledged byte counts.
        before = self.capture.last_cursor()
        self.record("tx_requested", details={"length": len(data)})
        sent = 0
        deadline = time.monotonic() + 3
        try:
            while sent < len(data):
                if self.stop.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("write stopped or exceeded send deadline")
                chunk = data[sent:sent + 4096]
                count = self.transport.write(chunk)
                if not count:
                    raise EOFError("transport accepted zero bytes")
                sent += count
                self.record("tx", chunk[:count])
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.requires_reconnect = self.kind in ("serial", "ssh")
            self.stop.set()
            # Some drivers cannot report how many bytes reached the peer on failure.
            # Never retry automatically: the command may already have taken effect.
            try:
                self.record("write_error", details={"error": str(exc), "known_sent": sent,
                                                     "delivery_uncertain": True})
            except Exception:
                LOG.exception("Unable to record write failure")
            return {"status": "write_error", "error": str(exc), "known_sent": sent,
                    "delivery_uncertain": True, "retry_safe": False, "cursor_before_send": before}
        return {"status": "sent", "bytes_sent": sent, "cursor_before_send": before,
                "meaning": "transport accepted bytes; device execution is not confirmed"}

    def send(self, data: bytes) -> dict:
        if not self.operation_lock.acquire(blocking=False):
            return {"status": "busy", "message": "another command or wait owns this session"}
        try:
            return self._send(data)
        finally:
            self.operation_lock.release()

    def expect(self, needle: str, after: int, timeout_seconds: float, hexadecimal: bool = False) -> dict:
        if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be 0..30")
        target = bytes.fromhex(needle) if hexadecimal else needle
        if not target or len(target) > 4096:
            raise ValueError("expected pattern must contain 1..4096 characters or bytes")
        deadline = time.monotonic() + timeout_seconds
        tail = self.capture.rx_tail(after, len(target) - 1, hexadecimal)
        cursor, output, scanned, matched_at = after, "", 0, None
        while True:
            page = self.read(cursor, 64, min(0.25, max(0, deadline - time.monotonic())))
            for event in page["events"]:
                cursor = event["seq"]
                if event["kind"] == "rx":
                    part = bytes.fromhex(event["hex"]) if hexadecimal else event["text"]
                    combined = tail + part
                    output = (output + event["text"])[-16384:]
                    scanned += len(part)
                    if target in combined:
                        matched_at = cursor
                        break
                    tail = combined[-(len(target) - 1):] if len(target) > 1 else combined[:0]
            if matched_at is not None:
                outcome = "matched"
            elif time.monotonic() >= deadline:
                outcome = "timeout"
            elif scanned >= 4_000_000:
                outcome = "scan_limit"
            elif self.state != "open" and not page["has_more"]:
                outcome = self.state
            else:
                continue
            return {"status": outcome, "matched": matched_at is not None, "matched_at_seq": matched_at,
                    "next_cursor": cursor, "rx_text_tail": output, "capture_id": self.id,
                    "error": self.error, "completion_is_success": False,
                    "meaning": "matching output does not establish an exit code or successful operation"}

    def command(self, data: bytes, expected: str | None, timeout_seconds: float, hex_pattern: bool) -> dict:
        # Validate before sending; invalid expectations must not cause a hardware write.
        if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be 0..30")
        if expected is not None:
            pattern = bytes.fromhex(expected) if hex_pattern else expected
            if not pattern or len(pattern) > 4096:
                raise ValueError("expected pattern must contain 1..4096 characters or bytes")
        if not self.operation_lock.acquire(blocking=False):
            return {"status": "busy", "message": "another command owns this session"}
        try:
            result = self._send(data)
            if result["status"] != "sent":
                return result
            if expected is None:
                return {**result, "result": self.read(result["cursor_before_send"], 64, 0),
                        "next_action": "read or expect from cursor_before_send; command completion is unknown"}
            return {"send": result, **self.expect(expected, result["cursor_before_send"], timeout_seconds, hex_pattern)}
        finally:
            self.operation_lock.release()

    def interrupt(self) -> dict:
        # Interrupt is intentionally allowed while command() waits for its prompt.
        return self._send(b"\x03")

    def control(self, dtr: bool | None, rts: bool | None) -> dict:
        result = self.control_sequence([{"dtr": dtr, "rts": rts, "delay_seconds": 0}], False)
        return {**result, "dtr": dtr, "rts": rts}

    def control_sequence(self, steps: list[dict], reapply_dtr_after_rts: bool) -> dict:
        if self.kind != "serial" or self.state != "open":
            raise ValueError("serial control lines require an open serial session")
        if not 1 <= len(steps) <= 32:
            raise ValueError("provide 1..32 control steps")
        duration = 0
        for step in steps:
            if set(step) - {"dtr", "rts", "delay_seconds"}:
                raise ValueError("step fields are dtr, rts and delay_seconds")
            for key in ("dtr", "rts"):
                if step.get(key) is not None and type(step[key]) is not bool:
                    raise ValueError("control levels must be booleans")
            delay = step.get("delay_seconds", 0)
            if not isinstance(delay, (int, float)) or not math.isfinite(delay) or not 0 <= delay <= 5:
                raise ValueError("step delay_seconds must be 0..5")
            if step.get("dtr") is None and step.get("rts") is None and delay == 0:
                raise ValueError("each step must set a level or wait")
            duration += delay
        if duration > 10:
            raise ValueError("total control sequence delay must not exceed 10 seconds")
        if not self.operation_lock.acquire(blocking=False):
            return {"status": "busy"}
        applied = 0
        before = None
        try:
            before = self.capture.last_cursor()
            with self.write_lock:
                for step in steps:
                    if self.stop.is_set():
                        raise ValueError("session is stopping")
                    self.record("control_requested", details={**step, "reapply_dtr_after_rts": reapply_dtr_after_rts})
                    self.transport.control(step.get("dtr"), step.get("rts"), reapply_dtr_after_rts)
                    self.record("control_applied", details=step)
                    applied += 1
                    if self.stop.wait(step.get("delay_seconds", 0)):
                        raise ValueError("session stopped during control sequence")
            return {"status": "applied", "steps_applied": applied, "cursor_before_control": before,
                    "device_ready": "unknown"}
        except Exception as exc:
            self.requires_reconnect = True
            try:
                self.record("control_error", details={"error": str(exc), "steps_applied": applied})
            except Exception:
                LOG.exception("Unable to record control failure")
            return {"status": "control_error", "error": str(exc), "steps_applied": applied,
                    "cursor_before_control": before, "delivery_uncertain": True, "retry_safe": False}
        finally:
            self.operation_lock.release()

    def close(self) -> dict:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=4)
        if self.thread and self.thread.is_alive():
            return {**self.status(), "status": "closing", "message": "reader is still stopping"}
        if self.cleanup_error and self.transport:
            try:
                self.transport.close()
                self.cleanup_error = None
            except Exception as exc:
                self.cleanup_error = str(exc)
                return {**self.status(), "status": "close_failed", "message": "transport release is not confirmed"}
        return {**self.status(), "status": "closed"}
