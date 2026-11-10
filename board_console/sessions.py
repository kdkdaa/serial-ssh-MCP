"""Live readers, serialized writes and condition-based waits."""

from __future__ import annotations

import codecs
import logging
import math
import threading
import time
from pathlib import Path

from .capture import Capture

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
        self.decoders = {k: codecs.getincrementaldecoder(encoding)(errors="replace") for k in ("rx", "tx")}

    def record(self, kind: str, data: bytes = b"", details: dict | None = None, text: str | None = None) -> int:
        with self.condition:
            if text is None:
                text = self.decoders[kind].decode(data) if kind in self.decoders else ""
            seq = self.capture.append(kind, data, text, time.monotonic_ns() - self.started_ns, details or {})
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
                self.capture.set_info({"state": state, "error": error, "ended_time_ns": time.time_ns()})
            except Exception:
                LOG.exception("Unable to save terminal state %s", self.id)
            self.condition.notify_all()

    def _receive(self) -> None:
        state, error = "closed", None
        try:
            while not self.stop.is_set():
                data = self.transport.read()
                if data:
                    self.record("rx", data)
        except Exception as exc:
            if not self.stop.is_set():
                state, error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            self.stop.set()
            with self.write_lock:
                try:
                    self.transport.close()
                except Exception:
                    LOG.exception("Unable to close transport %s", self.id)
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
                    "task_id": self.task_id, "requires_reconnect": self.requires_reconnect}

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
        if self.kind != "serial" or self.state != "open":
            raise ValueError("serial control lines require an open serial session")
        if dtr is None and rts is None:
            raise ValueError("specify dtr or rts explicitly")
        if not self.operation_lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            with self.write_lock:
                if self.stop.is_set():
                    raise ValueError("session is stopping")
                self.record("control_requested", details={"dtr": dtr, "rts": rts})
                self.transport.control(dtr, rts)
                self.record("control_applied", details={"dtr": dtr, "rts": rts})
            return {"status": "applied", "dtr": dtr, "rts": rts}
        finally:
            self.operation_lock.release()

    def close(self) -> dict:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=4)
        if self.thread and self.thread.is_alive():
            return {**self.status(), "status": "closing", "message": "reader is still stopping"}
        return {**self.status(), "status": "closed"}
