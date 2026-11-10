"""SSH identity checks and credential-free, structured connection failures."""

from __future__ import annotations

import base64
import errno
import hashlib
import re
import socket
import threading
import uuid
from pathlib import Path

import paramiko

TRUST_LOCK = threading.RLock()


class SSHFailure(RuntimeError):
    def __init__(self, code: str, message: str, status: str = "open_failed", **details):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details

    def result(self) -> dict:
        return {"status": self.status, "error_code": self.code, "message": str(self),
                "retry_safe": False, **self.details}


def fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")


def validate_target(host: str, port: int) -> None:
    if not host or any(c.isspace() or c in ",/\\@*?!|" for c in host) or not 1 <= port <= 65535:
        raise SSHFailure("invalid_parameters", "Supply an actual SSH hostname/IP and port 1..65535.", "error")


def classify_failure(exc: Exception, stage: str = "connect") -> SSHFailure:
    """Never return arbitrary exception text from authentication code or key files."""
    if isinstance(exc, SSHFailure):
        return exc
    if isinstance(exc, paramiko.BadAuthenticationType):
        return SSHFailure("authentication_method_unsupported", "Server does not accept the selected authentication method.",
                          "needs_credentials", allowed_methods=list(exc.allowed_types))
    if isinstance(exc, paramiko.PasswordRequiredException):
        return SSHFailure("key_passphrase_required", "The private key is encrypted; supply key_passphrase.",
                          "needs_credentials", missing_fields=["key_passphrase"])
    if isinstance(exc, paramiko.AuthenticationException):
        code = "authentication_timeout" if "timeout" in str(exc).lower() else "authentication_failed"
        return SSHFailure(code, "SSH authentication timed out." if code.endswith("timeout") else
                          "Server rejected authentication; check username and credentials. Do not retry unchanged credentials.")
    if isinstance(exc, socket.gaierror):
        return SSHFailure("name_resolution_failed", "SSH hostname could not be resolved.")
    if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        errors = list(exc.errors.values())
        codes = {getattr(e, "errno", None) for e in errors}
        if codes and codes <= {errno.ECONNREFUSED, 10061}:
            return SSHFailure("connection_refused", "All attempted SSH addresses refused the connection.")
        if errors and all(isinstance(e, TimeoutError) for e in errors):
            return SSHFailure("connection_timeout", "All attempted SSH addresses timed out.")
        return SSHFailure("network_unreachable", "Could not connect to any resolved SSH address.")
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return SSHFailure("connection_timeout" if stage == "connect" else "shell_timeout",
                          "SSH connection timed out." if stage == "connect" else "SSH terminal setup timed out.")
    if isinstance(exc, ConnectionRefusedError):
        return SSHFailure("connection_refused", "SSH port refused the connection.")
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return SSHFailure("local_file_error", "A requested SSH key or local trust file is missing or inaccessible.")
    if isinstance(exc, OSError):
        return SSHFailure("network_error", "SSH network operation failed.", os_error_code=exc.errno)
    if isinstance(exc, paramiko.SSHException):
        if str(exc) == "No authentication methods available":
            return SSHFailure("credentials_required", "No usable SSH authentication was found. Supply password or key_file.",
                              "needs_credentials", missing_fields=["password_or_key_file"])
        return SSHFailure("shell_open_failed" if stage == "shell" else "ssh_negotiation_failed",
                          "Server could not open an interactive terminal." if stage == "shell" else
                          "SSH negotiation or key authentication setup failed.")
    return SSHFailure("ssh_internal_error", "SSH setup failed; no credential-bearing exception text is returned.",
                      error_type=type(exc).__name__)


class HostTrust(paramiko.MissingHostKeyPolicy):
    """Verify EVERY negotiated key before authentication, including explicit pins.

    SSHClient's own host-key table stays empty so this policy is always consulted.
    Existing known_hosts entries are loaded here instead of accepting unknown keys.
    """
    def __init__(self, host: str, port: int, store: Path, known_hosts: str | None = None,
                 expected: str | None = None, remember: bool = False):
        validate_target(host, port)
        if expected is not None and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", expected):
            raise SSHFailure("invalid_fingerprint", "Use the full SHA256 fingerprint returned by ssh_probe/ssh_open.", "error")
        if remember and expected is None:
            raise SSHFailure("fingerprint_required", "remember_host_key requires an explicitly confirmed host_key_sha256.", "error")
        self.host, self.port, self.store = host, port, store
        self.hostname = host if port == 22 else f"[{host}]:{port}"
        self.expected, self.remember = expected, remember
        self.identity = None
        self.keys: list[paramiko.PKey] = []
        paths = [Path(known_hosts).expanduser()] if known_hosts else [Path.home() / ".ssh" / "known_hosts"]
        paths.append(store)
        try:
            with TRUST_LOCK:
                for path in dict.fromkeys(paths):
                    if not path.exists():
                        if known_hosts and path == paths[0]:
                            raise SSHFailure("known_hosts_missing", "The explicitly selected known_hosts file does not exist.")
                        continue
                    records = paramiko.HostKeys(str(path)).lookup(self.hostname)
                    if records:
                        self.keys.extend(records.values())
        except SSHFailure:
            raise
        except Exception:
            raise SSHFailure("trust_store_error", "Could not read or parse the SSH host-key records.") from None

    def describe(self, key: paramiko.PKey) -> dict:
        actual = fingerprint(key)
        identity = {"host": self.host, "port": self.port, "host_key_type": key.get_name(),
                    "host_key_sha256": actual}
        same_type = [k for k in self.keys if k.get_name() == key.get_name()]
        if any(k.asbytes() != key.asbytes() for k in same_type):
            raise SSHFailure("host_key_changed", "The observed host key conflicts with a saved key. Verify the change before updating the specific trust record.",
                              "host_key_changed", **identity,
                              saved_fingerprints=sorted({fingerprint(k) for k in same_type}))
        if self.expected is not None and actual != self.expected:
            raise SSHFailure("host_key_mismatch", "Server fingerprint differs from the explicitly confirmed fingerprint. Authentication was not attempted.",
                              "host_key_mismatch", **identity, expected_fingerprint=self.expected)
        if same_type:
            return {**identity, "trust_source": "known_hosts"}
        if self.expected is not None:
            return {**identity, "trust_source": "explicit_fingerprint"}
        raise SSHFailure("host_key_unknown", "Host identity needs confirmation before authentication.",
                          "needs_host_key_confirmation", **identity,
                          questions=[f"请核对 {self.host}:{self.port} 的 {key.get_name()} 指纹 {actual}，是否信任并保存？"],
                          next_action="After the user confirms this identity (or supplies an independently verified fingerprint), call ssh_open with host_key_sha256 and remember_host_key=true. Do not confirm merely because a probe returned it.")

    def transport_factory(self, sock, **kwargs):
        transport = paramiko.Transport(sock, **kwargs)
        options = transport.get_security_options()
        saved_types = {key.get_name() for key in self.keys}
        preferred = [name for name in options.key_types
                     if ("ssh-rsa" if name.startswith("rsa-sha2-") else name) in saved_types]
        # Prefer an already trusted key type without enabling any disabled algorithm.
        options.key_types = preferred + [name for name in options.key_types if name not in preferred]
        return transport

    def missing_host_key(self, client, hostname, key):
        self.identity = self.describe(key)
        if self.remember:
            self._save(key)
            self.identity["saved_to"] = str(self.store)

    def _save(self, key: paramiko.PKey) -> None:
        temporary = self.store.with_name(self.store.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with TRUST_LOCK:
                records = paramiko.HostKeys(str(self.store)) if self.store.exists() else paramiko.HostKeys()
                existing = records.lookup(self.hostname)
                previous = existing.get(key.get_name()) if existing else None
                if previous is not None and previous.asbytes() != key.asbytes():
                    raise SSHFailure("host_key_changed", "Saved host key changed while connecting; no key was overwritten.", "host_key_changed")
                records.add(self.hostname, key.get_name(), key)
                self.store.parent.mkdir(parents=True, exist_ok=True)
                records.save(str(temporary))
                temporary.replace(self.store)
        except SSHFailure:
            raise
        except Exception:
            raise SSHFailure("trust_store_error", "Could not save the confirmed host key; authentication was not attempted.") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def probe_host(host: str, port: int, store: Path, known_hosts: str | None = None) -> dict:
    """Perform TCP + SSH key exchange only. No username, password, auth or shell."""
    transport = None
    connection = None
    try:
        trust = HostTrust(host, port, store, known_hosts)
        connection = socket.create_connection((host, port), timeout=10)
        transport = trust.transport_factory(connection)
        transport.banner_timeout = 10
        transport.start_client(timeout=10)
        identity = trust.describe(transport.get_remote_server_key())
        return {"status": "host_key_verified", **identity, "authenticated": False, "trust_saved": False}
    except Exception as exc:
        return {**classify_failure(exc).result(), "authenticated": False, "trust_saved": False}
    finally:
        if transport:
            transport.close()
        if connection:
            connection.close()
