"""Bounded, durable byte recordings with independent reader cursors."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class Capture:
    def __init__(self, path: Path, metadata: dict | None = None, max_bytes: int = 268435456):
        self.path = path
        self.lock = threading.RLock()
        self.max_bytes = max_bytes
        self.used_bytes = 0
        self.readonly = metadata is None
        if self.readonly:
            self.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        else:
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.executescript("""
                CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    time_ns INTEGER NOT NULL, elapsed_ns INTEGER NOT NULL,
                    kind TEXT NOT NULL, data BLOB NOT NULL, text TEXT NOT NULL,
                    details TEXT NOT NULL
                );
            """)
            self.set_info(metadata)
        self.db.row_factory = sqlite3.Row

    def set_info(self, values: dict) -> None:
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO info VALUES (?, ?)",
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()],
            )

    def info(self) -> dict:
        with self.lock:
            result = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT * FROM info")}
            result["last_cursor"] = self.last_cursor()
            result["path"] = str(self.path)
            return result

    def append(self, kind: str, data: bytes, text: str, elapsed_ns: int, details: dict) -> int:
        detail_json = json.dumps(details, ensure_ascii=False)
        size = len(data) + len(text.encode("utf-8")) + len(detail_json.encode("utf-8")) + 128
        with self.lock:
            if self.used_bytes + size > self.max_bytes:
                raise RuntimeError("capture_limit_reached: recording stopped; open a new session")
            with self.db:
                cursor = self.db.execute(
                    "INSERT INTO events(time_ns, elapsed_ns, kind, data, text, details) VALUES (?, ?, ?, ?, ?, ?)",
                    (time.time_ns(), elapsed_ns, kind, data, text, detail_json),
                )
            self.used_bytes += size
            return int(cursor.lastrowid)

    def last_cursor(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

    def rx_tail(self, at: int, length: int, binary: bool = False):
        """Carry a partial match across paginated expect/search calls."""
        if length <= 0:
            return b"" if binary else ""
        column = "data" if binary else "text"
        parts, count = [], 0
        with self.lock:
            for row in self.db.execute(
                f"SELECT {column} FROM events WHERE seq <= ? AND kind = 'rx' AND length({column}) > 0 ORDER BY seq DESC",
                (at,),
            ):
                part = bytes(row[0]) if binary else row[0]
                parts.append(part)
                count += len(part)
                if count >= length:
                    break
        return (b"" if binary else "").join(reversed(parts))[-length:]

    def page(self, after: int = 0, limit: int = 64, byte_budget: int = 32768) -> dict:
        if after < 0 or not 1 <= limit <= 256:
            raise ValueError("cursor must be >= 0; limit must be 1..256")
        with self.lock:
            last = self.last_cursor()
            if after > last:
                raise ValueError("cursor exceeds this capture's last_cursor")
            rows = self.db.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit))
            events, size = [], 0
            for row in rows:
                data = bytes(row["data"])
                cost = len(data) + len(row["text"].encode("utf-8")) + len(row["details"])
                if events and size + cost > byte_budget:
                    break
                events.append({
                    "seq": row["seq"], "time_ns": row["time_ns"], "elapsed_ns": row["elapsed_ns"],
                    "kind": row["kind"], "hex": data.hex(), "text": row["text"],
                    "details": json.loads(row["details"]),
                })
                size += cost
            next_cursor = events[-1]["seq"] if events else after
            return {
                "events": events, "next_cursor": next_cursor, "last_cursor": last,
                "has_more": next_cursor < last,
                "rx_text": "".join(e["text"] for e in events if e["kind"] == "rx"),
            }

    def search(self, query: str, after: int = 0, scan_limit: int = 512, match_limit: int = 20) -> dict:
        """Literal, case-insensitive search, including matches across RX chunks."""
        if not query or len(query) > 1024:
            raise ValueError("query must contain 1..1024 characters")
        if after < 0 or not 1 <= scan_limit <= 4096 or not 1 <= match_limit <= 100:
            raise ValueError("invalid cursor or search limits")
        with self.lock:
            last = self.last_cursor()
            if after > last:
                raise ValueError("cursor exceeds this capture's last_cursor")
            # Carry enough text from prior RX events to find a boundary-spanning match.
            tail = self.rx_tail(after, len(query) + 160)
            matches, cursor, scanned_bytes = [], after, 0
            needle = query.lower()
            for row in self.db.execute("SELECT seq, kind, text FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, scan_limit)):
                cursor = row["seq"]
                if row["kind"] == "rx":
                    combined = tail + row["text"]
                    start = max(0, len(tail) - len(query) + 1)
                    index = combined.lower().find(needle, start)
                    if index >= 0:
                        matches.append({"ending_seq": cursor, "context": combined[max(0, index - 160):index + len(query) + 320]})
                    tail = combined[-(len(query) + 160):]
                scanned_bytes += len(row["text"])
                if len(matches) >= match_limit or scanned_bytes >= 2_000_000:
                    break
            return {"matches": matches, "next_cursor": cursor, "has_more": cursor < last,
                    "scope": "RX text, literal case-insensitive; at most one match per event"}

    def close(self) -> None:
        with self.lock:
            self.db.close()
