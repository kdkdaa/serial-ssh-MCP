"""Declarative, bounded deployment and verification plans."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Nonempty = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Checks(Model):
    exit_code: int = Field(default=0, ge=0, le=255)
    contains: list[Nonempty] = Field(default_factory=list, max_length=32)
    not_contains: list[Nonempty] = Field(default_factory=list, max_length=32)
    json_equals: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_pointers(self):
        if len(self.json_equals) > 32:
            raise ValueError("at most 32 JSON assertions")
        if any(key and not key.startswith("/") for key in self.json_equals):
            raise ValueError("json_equals keys must be JSON pointers: /version, /items/0 or empty string")
        if any(re.search(r"~(?![01])", key) for key in self.json_equals):
            raise ValueError("JSON pointer escapes must be ~0 or ~1")
        return self


class CommandStep(Model):
    kind: Literal["command"] = "command"
    name: Nonempty
    phase: Literal["prepare", "restart", "verify", "diagnose"]
    command: Nonempty
    timeout_seconds: float = Field(default=15, gt=0, le=300, allow_inf_nan=False)
    checks: Checks = Field(default_factory=Checks)


class ReadyStep(Model):
    kind: Literal["wait_ready"] = "wait_ready"
    name: Nonempty
    command: Nonempty
    timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    probe_timeout_seconds: float = Field(default=3, gt=0, le=30, allow_inf_nan=False)
    interval_seconds: float = Field(default=0.3, ge=0.05, le=5, allow_inf_nan=False)
    checks: Checks = Field(default_factory=Checks)


class UploadStep(Model):
    kind: Literal["upload"] = "upload"
    name: Nonempty
    local_path: Nonempty
    remote_path: Nonempty
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: str = Field(default="0755", pattern=r"^0?[0-7]{3}$")
    timeout_seconds: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)

    @model_validator(mode="after")
    def absolute_remote(self):
        if not self.remote_path.startswith("/") or self.remote_path.endswith("/"):
            raise ValueError("remote_path must be an absolute POSIX file path")
        if self.remote_path.split("/")[-1] == ".":
            raise ValueError("remote_path must name a file, not a directory")
        if ".." in self.remote_path.split("/") or "\x00" in self.remote_path or "\n" in self.remote_path or "\r" in self.remote_path:
            raise ValueError("remote_path contains traversal or control characters")
        return self


Step = Annotated[CommandStep | ReadyStep | UploadStep, Field(discriminator="kind")]


class Plan(Model):
    name: Nonempty
    change_summary: Nonempty
    shell: Literal["posix"] = "posix"
    newline: Literal["cr", "lf", "crlf"]
    timeout_seconds: float = Field(default=180, gt=0, le=1800, allow_inf_nan=False)
    steps: list[Step] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def valid_steps(self):
        if len({s.name for s in self.steps}) != len(self.steps):
            raise ValueError("step names must be unique")
        if not any(isinstance(s, CommandStep) and s.phase == "verify" for s in self.steps):
            raise ValueError("include at least one verify step that checks this change")
        for step in self.steps:
            if hasattr(step, "command") and "\x00" in step.command:
                raise ValueError("commands cannot contain NUL")
        return self
