"""Small runtime helpers shared by source and bundled entry points."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    return Path(__file__).resolve().parents[1]


def install_root() -> Path:
    return Path(sys.executable).resolve().parent if frozen() else resource_root()


def server_command() -> list[str]:
    if frozen():
        return [str(Path(sys.executable).resolve())]
    return [str(Path(sys.executable).resolve()), str(resource_root() / "launch.py")]


def worker_command() -> list[str]:
    if frozen():
        return [str(Path(sys.executable).resolve()), "--process-worker", str(os.getpid())]
    # The real interpreter avoids a Windows venv redirector owning the wrong PID.
    return [getattr(sys, "_base_executable", sys.executable), "-I", "-u", "-X", "utf8",
            str(Path(__file__).with_name("process_worker.py")), str(os.getpid())]
