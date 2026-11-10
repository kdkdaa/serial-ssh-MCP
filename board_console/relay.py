"""Opaque session input and bounded output collection, with no command semantics."""

from __future__ import annotations

import math
import time

from .sessions import Session


def exchange(session: Session, data: bytes | None, after: int | None,
             wait_seconds: float, quiet_seconds: float, limit: int) -> dict:
    # Validate transport/collection parameters only, never target command syntax.
    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 30:
        raise ValueError("wait_seconds must be 0..30")
    if not math.isfinite(quiet_seconds) or not 0 <= quiet_seconds <= 5:
        raise ValueError("quiet_seconds must be 0..5")
    if not 1 <= limit <= 256:
        raise ValueError("limit must be 1..256 events")
    if after is not None:
        session.capture.page(after, 1)  # Invalid cursor must fail before sending.
    owns_lock = data is not None
    if owns_lock and not session.operation_lock.acquire(blocking=False):
        return {"status": "busy", "session_id": session.id, "task_id": session.task_id,
                "sent": False, "message": "another exchange, command or task owns this session"}
    started = time.monotonic()
    sent = None
    cursor = after if after is not None else 0
    start_cursor = cursor
    text_parts, hex_parts = [], []
    try:
        if data is not None:
            sent = session._send(data)
            if sent["status"] != "sent":
                return {**sent, "session_id": session.id, "capture_id": session.id,
                        "error_source": "transport", "completion": "unknown",
                        "duration_seconds": round(time.monotonic() - started, 4)}
        cursor = after if after is not None else (sent["cursor_before_send"] if sent else 0)
        start_cursor = cursor
        # The receive budget starts after sending; it never means the command stopped.
        deadline = time.monotonic() + wait_seconds
        last_rx = None
        scanned, cost, received_bytes = 0, 0, 0
        reason = "timeout"
        wait = 0.0
        while True:
            page = session.read(cursor, min(64, limit - scanned), wait)
            budget_full = False
            for event in page["events"]:
                event_cost = len(event["hex"]) + len(event["text"].encode("utf-8")) if event["kind"] == "rx" else 0
                if cost + event_cost > 65536:
                    budget_full = True
                    break  # Leave this event unread; next_cursor must not skip it.
                cursor = event["seq"]
                scanned += 1
                if event["kind"] == "rx":
                    text_parts.append(event["text"])
                    hex_parts.append(event["hex"])
                    received_bytes += len(event["hex"]) // 2
                    cost += event_cost
                    last_rx = time.monotonic()
            if budget_full or scanned >= limit:
                reason = "limit"
                break
            now = time.monotonic()
            if session.state != "open" and not page["has_more"]:
                reason = "session_ended"
                break
            if now >= deadline:
                reason = "timeout"
                break
            if page["has_more"]:
                wait = 0.0
                continue
            if last_rx is not None and now >= last_rx + quiet_seconds:
                reason = "quiet"
                break
            until = min(deadline, last_rx + quiet_seconds) if last_rx is not None else deadline
            wait = min(0.25, max(0, until - now))
        state = session.status()
        return {"status": "received" if last_rx is not None else "no_data",
                "session_id": session.id, "capture_id": session.id,
                "session_state": state["state"], "error": state["error"],
                "error_source": "session" if state["error"] else None,
                "requires_reconnect": state["requires_reconnect"],
                "send": sent, "rx_text": "".join(text_parts), "rx_hex": "".join(hex_parts),
                "received_bytes": received_bytes, "start_cursor": start_cursor, "next_cursor": cursor,
                "has_more": cursor < state["last_cursor"],
                "may_receive_more": state["state"] == "open" and not session.stop.is_set(),
                "stop_reason": reason, "completion": "unknown",
                "duration_seconds": round(time.monotonic() - started, 4),
                "meaning": "raw session output, including echo/background logs; quiet or timeout is not completion"}
    except Exception as exc:
        if sent is None:
            raise
        # A post-send collection error must not erase the evidence of a completed write.
        return {"status": "collection_error", "error_source": "collection", "message": str(exc),
                "send": sent, "session_id": session.id, "capture_id": session.id,
                "start_cursor": start_cursor, "next_cursor": cursor,
                "rx_text": "".join(text_parts), "rx_hex": "".join(hex_parts),
                "completion": "unknown", "retry_safe": False,
                "duration_seconds": round(time.monotonic() - started, 4)}
    finally:
        if owns_lock:
            session.operation_lock.release()
