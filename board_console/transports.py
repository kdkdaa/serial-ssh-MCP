"""Transport adapters. No implicit baud probing or host-key acceptance."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import paramiko
import serial
from serial.tools import list_ports

from .ssh_auth import HostTrust, SSHFailure, classify_failure


def serial_ports() -> list[dict]:
    return [{"port": p.device, "description": p.description, "vid": p.vid, "pid": p.pid,
             "serial_number": p.serial_number, "location": p.location, "hwid": p.hwid}
            for p in list_ports.comports()]


class SerialTransport:
    def __init__(self, port: str, baudrate: int, bytesize: int, parity: str, stopbits: float,
                 dtr: bool, rts: bool, rtscts: bool):
        if not port or "://" in port:
            raise ValueError("a physical serial port is required; URL handlers are not enabled")
        if baudrate <= 0 or bytesize not in (5, 6, 7, 8) or parity not in ("N", "E", "O", "M", "S") or stopbits not in (1, 1.5, 2):
            raise ValueError("invalid serial settings")
        self.port = serial.Serial(port=None, baudrate=baudrate, bytesize=bytesize, parity=parity,
                                  stopbits=stopbits, timeout=0.2, write_timeout=3, rtscts=rtscts)
        self.port.dtr = dtr
        self.port.rts = rts
        self.port.port = port
        try:
            self.port.open()
        except Exception:
            self.port.close()
            raise

    def read(self) -> bytes:
        return self.port.read(min(max(self.port.in_waiting, 1), 4096))

    def write(self, data: bytes) -> int:
        return self.port.write(data)

    def control(self, dtr: bool | None, rts: bool | None, reapply_dtr_after_rts: bool = False) -> None:
        if dtr is not None:
            self.port.dtr = dtr
        if rts is not None:
            self.port.rts = rts
            if reapply_dtr_after_rts:
                self.port.dtr = self.port.dtr  # usbser SET_CONTROL_LINE_STATE workaround.

    def close(self) -> None:
        self.port.close()


class SSHTransport:
    def __init__(self, host: str, username: str, port: int, key_file: str | None,
                 known_hosts: str | None, password_env: str | None,
                 password: str | None = None, host_key_sha256: str | None = None,
                 remember_host_key: bool = False, trust_store: Path | None = None,
                 key_passphrase: str | None = None):
        self.client = paramiko.SSHClient()
        stage = "connect"
        trust = None
        try:
            if not username:
                raise SSHFailure("invalid_parameters", "An SSH username is required.", "error")
            if password is not None and password_env is not None:
                raise SSHFailure("invalid_parameters", "Choose password or password_env, not both.", "error")
            if password_env is not None:
                password = os.environ.get(password_env)
                if password is None:
                    raise SSHFailure("credentials_unavailable", "The named password variable is absent. Supply password directly or use an existing key; changing environment variables is not required.",
                                     "needs_credentials", missing_fields=["password_or_key_file"])
            trust = HostTrust(host, port, trust_store or Path.home() / ".ssh" / "board_console_known_hosts",
                              known_hosts, host_key_sha256, remember_host_key)
            self.client.set_missing_host_key_policy(trust)
            # Explicit credentials bypass unrelated agent/default-key attempts.
            auto_keys = password is None and key_file is None
            self.client.connect(hostname=host, port=port, username=username,
                                key_filename=str(Path(key_file).expanduser()) if key_file else None,
                                password=password, passphrase=key_passphrase,
                                timeout=10, banner_timeout=10, auth_timeout=10, channel_timeout=10,
                                allow_agent=auto_keys, look_for_keys=auto_keys,
                                transport_factory=trust.transport_factory)
            self.host_key = trust.identity
            connection = self.client.get_transport()
            if connection is None or not connection.is_active():
                raise SSHFailure("connection_lost", "SSH connection ended before opening the terminal.")
            if not connection.is_authenticated():
                raise SSHFailure("authentication_incomplete", "Server requires additional authentication; arbitrary interactive MFA is not supported.",
                                 "needs_credentials")
            connection.set_keepalive(15)
            stage = "shell"
            self.channel = self.client.invoke_shell(term="vt100", width=120, height=40)
            self.channel.settimeout(0.2)
        except Exception as exc:
            self.client.close()
            failure = classify_failure(exc, stage)
            if trust and trust.identity:
                failure.details["host_key"] = trust.identity
            raise failure from None

    def read(self) -> bytes:
        try:
            data = self.channel.recv(4096)
        except socket.timeout:
            transport = self.client.get_transport()
            if transport is None or not transport.is_active():
                raise EOFError("SSH connection lost")
            return b""
        if not data:
            raise EOFError("SSH shell closed")
        return data

    def write(self, data: bytes) -> int:
        return self.channel.send(data)

    def read_file(self, path: str, offset: int, limit: int) -> dict:
        if not path.startswith("/") or offset < 0 or not 1 <= limit <= 65536:
            raise ValueError("use an absolute remote POSIX path, offset >= 0 and limit 1..65536")
        with self.client.open_sftp() as sftp:
            sftp.get_channel().settimeout(10)
            with sftp.open(path, "rb") as stream:
                stream.seek(offset)
                data = stream.read(limit)
                size = stream.stat().st_size
        return {"remote_path": path, "offset": offset, "next_offset": offset + len(data),
                "has_more": offset + len(data) < size, "hex": data.hex(), "text": data.decode("utf-8", "replace")}

    def close(self) -> None:
        self.client.close()
