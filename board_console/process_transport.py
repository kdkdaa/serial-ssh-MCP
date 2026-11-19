"""Persistent piped host tools, with bounded reads/writes and owned process trees."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
from pathlib import Path

from .runtime import worker_command


class ProcessExited(EOFError):
    def __init__(self, returncode: int):
        self.returncode = returncode
        super().__init__(f"host tool exited with code {returncode}")


class ProcessTransport:
    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str | None]):
        if not argv or any(not isinstance(a, str) or "\0" in a for a in argv) or not argv[0]:
            raise ValueError("argv must contain an executable and literal arguments")
        if Path(argv[0]).suffix.lower() in (".cmd", ".bat"):
            raise ValueError("batch files require an explicit shell argv; see board_startup_prepare")
        if not cwd.is_dir():
            raise ValueError("cwd must be an existing project directory")
        environment = os.environ.copy()
        for key, value in env.items():
            if not key or "=" in key or "\0" in key or (value is not None and "\0" in value):
                raise ValueError("invalid child environment entry")
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        self.pending = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.eof = threading.Event()
        self.reader_error = None
        self.close_lock = threading.Lock()
        self.closed = False
        self.process = subprocess.Popen(
            [*worker_command(), *argv],
            cwd=cwd, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.reader = threading.Thread(target=self._pump, daemon=True)
        try:
            self.reader.start()
        except Exception:
            self.close()
            raise

    def _pump(self):
        try:
            while not self.stopping.is_set():
                data = self.process.stdout.read(4096)
                if not data:
                    break
                while not self.stopping.is_set():
                    try:
                        self.pending.put(data, timeout=0.2)
                        break
                    except queue.Full:
                        pass  # Backpressure, never silently discard a live log.
        except Exception as exc:
            if not self.stopping.is_set():
                self.reader_error = exc
        finally:
            self.eof.set()

    def read(self) -> bytes:
        try:
            return self.pending.get(timeout=0.2)
        except queue.Empty:
            if self.reader_error:
                raise self.reader_error
            code = self.process.poll()
            if code is not None and os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if self.eof.is_set() and code is not None:
                raise ProcessExited(code)
            return b""

    def write(self, data: bytes) -> int:
        # A child that stops reading must not hang an MCP request indefinitely.
        done = threading.Event()
        result = []

        def write():
            try:
                result.append(self.process.stdin.write(data))
            except Exception as exc:
                result.append(exc)
            finally:
                done.set()

        threading.Thread(target=write, daemon=True).start()
        if not done.wait(2):
            self.close()
            raise TimeoutError("tool stdin blocked; process tree stopped; delivery uncertain")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    def status(self) -> dict:
        return {"supervisor_pid": self.process.pid, "exit_code": self.process.poll(),
                "stdio": "pipes; stdout and stderr merged", "pty": False}

    def close(self) -> None:
        with self.close_lock:
            if self.closed:
                return
            self.stopping.set()
            if os.name == "nt":
                if self.process.poll() is None:
                    self.process.kill()  # Supervisor Job closes, killing its descendants.
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=2)
            if self.reader.ident is not None:
                self.reader.join(timeout=1)
            self.process.stdin.close()
            self.process.stdout.close()
            self.closed = True
