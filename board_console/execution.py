"""Bounded POSIX-shell transactions and deterministic assertions."""

from __future__ import annotations

import json
import re
import shlex
import threading
import time
import uuid

from .sessions import Session
from .task_models import Checks


class ExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def checkpoint(cancel: threading.Event, deadline: float) -> None:
    if cancel.is_set():
        raise ExecutionError("cancelled", "task cancellation requested")
    if time.monotonic() >= deadline:
        raise ExecutionError("timeout", "task or step deadline exceeded")


def shell_wire(command: str, newline: str, encoding: str, token: str) -> tuple[bytes, str, str]:
    if "sh\r\n".encode(encoding) != b"sh\r\n":
        raise ValueError("POSIX shell tasks require an ASCII-compatible session encoding, such as UTF-8")
    begin, end = f"BC_BEGIN_{token}", f"BC_END_{token}"
    # Actual RS/US framing is produced by printf, never present in terminal echo.
    wire = (f"printf '\\036%s\\037\\n' {shlex.quote(begin)}; "
            f"sh -c {shlex.quote(command)}; "
            f"printf '\\036%s:%d\\037\\n' {shlex.quote(end)} \"$?\"")
    data = (wire + {"cr": "\r", "lf": "\n", "crlf": "\r\n"}[newline]).encode(encoding)
    if len(data) > 3500:
        raise ValueError("shell transaction exceeds 3500 bytes; upload a script and invoke it instead")
    return data, "\x1e" + begin + "\x1f", "\x1e" + end + ":"


def run_shell(session: Session, command: str, newline: str, cancel: threading.Event,
              deadline: float) -> dict:
    """Caller holds session.operation_lock for the entire task, not just this step."""
    checkpoint(cancel, deadline)
    token = uuid.uuid4().hex
    data, begin, end = shell_wire(command, newline, session.encoding, token)
    started = time.monotonic()
    sent = session._send(data)
    if sent["status"] != "sent":
        session.requires_reconnect = True
        raise ExecutionError("delivery_uncertain", "command delivery failed; do not retry automatically",
                             {**sent, "capture_id": session.id, "requires_reconnect": True})
    cursor = sent["cursor_before_send"]
    first_cursor = cursor
    text = ""
    pattern = re.compile(re.escape(end) + r"([0-9]{1,3})\x1f")
    try:
        while True:
            checkpoint(cancel, deadline)
            page = session.read(cursor, 128, min(0.2, max(0, deadline - time.monotonic())))
            cursor = page["next_cursor"]
            text += page["rx_text"]
            if len(text.encode("utf-8")) > 1_048_576:
                raise ExecutionError("output_limit", "command output exceeded 1 MiB; assertions were not evaluated")
            start = text.find(begin)
            match = pattern.search(text, start + len(begin)) if start >= 0 else None
            if match:
                output = text[start + len(begin):match.start()].removeprefix("\r\n").removeprefix("\n")
                return {"exit_code": int(match.group(1)), "stdout": output,
                        "duration_seconds": round(time.monotonic() - started, 4),
                        "capture_id": session.id, "start_cursor": first_cursor, "end_cursor": cursor,
                        "output_scope": "merged terminal output between unique command markers"}
            if session.state != "open" and not page["has_more"]:
                raise ExecutionError("connection_lost", session.error or "connection ended before command completion")
    except Exception as exc:
        if not isinstance(exc, ExecutionError):
            exc = ExecutionError("transaction_failed", str(exc))
        session.requires_reconnect = True
        interrupted = None
        if session.state == "open" and not session.stop.is_set():
            try:
                interrupted = session.interrupt()
            except Exception as interrupt_error:
                interrupted = {"error": str(interrupt_error)}
        exc.evidence.update({"capture_id": session.id, "start_cursor": first_cursor, "end_cursor": cursor,
                             "duration_seconds": round(time.monotonic() - started, 4),
                             "stdout_tail": text[-8192:], "interrupt": interrupted,
                             "requires_reconnect": True, "delivery_uncertain": True})
        raise exc


def json_equal(actual, expected) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(json_equal(actual[k], expected[k]) for k in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(json_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def evaluate(result: dict, checks: Checks) -> list[dict]:
    output = result["stdout"]
    assertions = [{"check": "exit_code", "expected": checks.exit_code,
                   "actual": result["exit_code"], "passed": result["exit_code"] == checks.exit_code}]
    assertions.extend({"check": "contains", "expected": value, "passed": value in output} for value in checks.contains)
    assertions.extend({"check": "not_contains", "expected": value, "passed": value not in output} for value in checks.not_contains)
    if checks.json_equals:
        try:
            document = json.loads(output)
        except (ValueError, TypeError) as exc:
            assertions.append({"check": "json_parse", "passed": False, "error": str(exc)})
            return assertions
        for pointer, expected in checks.json_equals.items():
            try:
                actual = document
                for key in pointer.split("/")[1:] if pointer else []:
                    key = key.replace("~1", "/").replace("~0", "~")
                    if isinstance(actual, list) and not re.fullmatch(r"0|[1-9][0-9]*", key):
                        raise ValueError("invalid JSON array index")
                    actual = actual[int(key)] if isinstance(actual, list) else actual[key]
                assertions.append({"check": "json_equals", "pointer": pointer, "expected": expected,
                                   "actual": actual, "passed": json_equal(actual, expected)})
            except (KeyError, IndexError, ValueError, TypeError):
                assertions.append({"check": "json_equals", "pointer": pointer, "expected": expected,
                                   "passed": False, "error": "path not found"})
    return assertions
