"""Artifact transfers publish only after verifying bytes on the target."""

from __future__ import annotations

import base64
import hashlib
import shlex
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from paramiko import SFTPClient

from .execution import ExecutionError, checkpoint, run_shell
from .sessions import Session
from .task_models import UploadStep


@contextmanager
def task_sftp(session: Session, cancel: threading.Event, deadline: float):
    """Bound subsystem negotiation as well as file I/O; close only this channel."""
    checkpoint(cancel, deadline)
    channel = session.transport.client.get_transport().open_session(
        timeout=min(3, max(0.01, deadline - time.monotonic())))
    channel.settimeout(min(3, max(0.01, deadline - time.monotonic())))
    finished = threading.Event()

    def guard():
        while not finished.wait(0.1):
            if cancel.is_set() or time.monotonic() >= deadline:
                channel.close()
                return

    watcher = threading.Thread(target=guard, name="sftp-deadline", daemon=True)
    try:
        watcher.start()
        channel.invoke_subsystem("sftp")
        with SFTPClient(channel) as sftp:
            yield sftp
    finally:
        finished.set()
        channel.close()
        if watcher.ident is not None:
            watcher.join(timeout=0.2)


def artifact(workspace: Path, step: UploadStep, kind: str) -> dict:
    path = (workspace / step.local_path).resolve()
    if not path.is_relative_to(workspace) or not path.is_file():
        raise ValueError(f"artifact must be a file inside workspace: {step.local_path}")
    size = path.stat().st_size
    maximum = 1_048_576 if kind == "serial" else 268_435_456
    if not 0 < size <= maximum:
        raise ValueError(f"artifact size must be 1..{maximum} bytes for {kind}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != step.sha256:
        raise ValueError(f"artifact SHA-256 changed: {step.local_path}; actual={digest}")
    return {"local_path": str(path), "remote_path": step.remote_path, "sha256": digest, "size": size}


def upload(session: Session, workspace: Path, step: UploadStep, newline: str,
           cancel: threading.Event, deadline: float) -> dict:
    checkpoint(cancel, deadline)
    evidence = artifact(workspace, step, session.kind)
    target = PurePosixPath(step.remote_path)
    staging = str(target.parent / f".{target.name}.board-console-{uuid.uuid4().hex}.part")
    evidence.update({"staging_path": staging, "published": False,
                     "capture_id": session.id, "start_cursor": session.capture.last_cursor()})
    started = time.monotonic()
    publishing = False

    def command(text: str) -> dict:
        result = run_shell(session, text, newline, cancel, deadline)
        if result["exit_code"] != 0:
            raise ExecutionError("upload_command_failed", "target rejected an upload operation", result)
        return result

    try:
        command(f"test -d {shlex.quote(str(target.parent))} && test ! -d {shlex.quote(step.remote_path)}")
        digest = hashlib.sha256()
        count = 0
        if session.kind == "ssh":
            with task_sftp(session, cancel, deadline) as sftp:
                with sftp.open(staging, "wx") as remote, Path(evidence["local_path"]).open("rb") as source:
                    while block := source.read(32768):
                        checkpoint(cancel, deadline)
                        remote.write(block)
                        digest.update(block)
                        count += len(block)
                        if count > evidence["size"]:
                            raise ExecutionError("artifact_changed", "artifact grew during upload")
                if sftp.stat(staging).st_size != count:
                    raise ExecutionError("upload_size_mismatch", "remote staging size differs from transmitted bytes")
        else:
            # Small binaries on Linux serial consoles: portable, bounded fallback.
            command(f"(set -C; umask 077; : > {shlex.quote(staging)})")
            with Path(evidence["local_path"]).open("rb") as source:
                while block := source.read(768):
                    checkpoint(cancel, deadline)
                    encoded = base64.b64encode(block).decode("ascii")
                    command(f"printf '%s' {shlex.quote(encoded)} | base64 -d >> {shlex.quote(staging)}")
                    digest.update(block)
                    count += len(block)
                    if count > evidence["size"]:
                        raise ExecutionError("artifact_changed", "artifact grew during upload")
        if count != evidence["size"] or digest.hexdigest() != step.sha256:
            raise ExecutionError("artifact_changed", "artifact changed during upload; original target was not replaced")
        verified = command(f"sha256sum {shlex.quote(staging)}")
        if not any(line.split() and line.split()[0] == step.sha256 for line in verified["stdout"].splitlines()):
            raise ExecutionError("remote_hash_mismatch", "target SHA-256 did not match", verified)
        checkpoint(cancel, deadline)
        publishing = True
        command(f"chmod {step.mode} {shlex.quote(staging)} && "
                f"test ! -d {shlex.quote(step.remote_path)} && "
                f"mv -f {shlex.quote(staging)} {shlex.quote(step.remote_path)}")
        evidence.update({"published": True, "bytes_sent": count, "remote_hash_verified": True,
                         "end_cursor": session.capture.last_cursor(),
                         "duration_seconds": round(time.monotonic() - started, 4)})
        return evidence
    except Exception as exc:
        if not isinstance(exc, ExecutionError):
            try:
                checkpoint(cancel, deadline)
            except ExecutionError as stop_error:
                exc = stop_error
            else:
                exc = ExecutionError("upload_failed", str(exc))
        evidence["end_cursor"] = session.capture.last_cursor()
        evidence["publication_may_have_occurred"] = publishing and exc.evidence.get("delivery_uncertain", False)
        exc.evidence = {**evidence, **exc.evidence}
        # Leave the identified staging file as evidence; never roll back blindly.
        raise exc
