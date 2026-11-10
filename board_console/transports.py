"""Transport adapters. No implicit baud probing or host-key acceptance."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import paramiko
import serial
from serial.tools import list_ports


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

    def control(self, dtr: bool | None, rts: bool | None) -> None:
        if dtr is not None:
            self.port.dtr = dtr
        if rts is not None:
            self.port.rts = rts

    def close(self) -> None:
        self.port.close()


class SSHTransport:
    def __init__(self, host: str, username: str, port: int, key_file: str | None,
                 known_hosts: str | None, password_env: str | None):
        if not host or not username or not 1 <= port <= 65535:
            raise ValueError("host, username and valid SSH port are required")
        password = None
        if password_env:
            password = os.environ.get(password_env)
            if password is None:
                raise ValueError("password environment variable is not present in the server process")
        self.client = paramiko.SSHClient()
        host_file = Path(known_hosts).expanduser() if known_hosts else Path.home() / ".ssh" / "known_hosts"
        if not host_file.is_file():
            raise ValueError("known_hosts file is missing; verify the host via your SSH client first")
        self.client.load_host_keys(str(host_file))
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            self.client.connect(hostname=host, port=port, username=username,
                                key_filename=str(Path(key_file).expanduser()) if key_file else None,
                                password=password, timeout=10, banner_timeout=10, auth_timeout=10, channel_timeout=10,
                                allow_agent=True, look_for_keys=True)
            self.client.get_transport().set_keepalive(15)
            self.channel = self.client.invoke_shell(term="vt100", width=120, height=40)
            self.channel.settimeout(0.2)
        except Exception:
            self.client.close()
            raise

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
