"""Board connection and explicit host-tool startup data; no firmware command parser."""

from __future__ import annotations

import codecs
import re
import string
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SerialConnection(Model):
    transport: Literal["serial"]
    baudrate: int | None = Field(default=None, gt=0)
    bytesize: Literal[5, 6, 7, 8] = 8
    parity: Literal["N", "E", "O", "M", "S"] = "N"
    stopbits: Literal[1, 1.5, 2] = 1
    dtr: bool = False
    rts: bool = False
    rtscts: bool = False


class SSHConnection(Model):
    transport: Literal["ssh"]
    port: int = Field(default=22, ge=1, le=65535)


class Forwarding(Model):
    encoding: str = "utf-8"
    data_format: Literal["text", "hex"] = "text"
    newline: Literal["none", "lf", "cr", "crlf"] = "none"

    @field_validator("encoding")
    @classmethod
    def known_encoding(cls, value: str) -> str:
        codecs.lookup(value)
        return value

    @model_validator(mode="after")
    def hex_ending(self):
        if self.data_format == "hex" and self.newline != "none":
            raise ValueError("hex profiles require newline=none; include endings in payload bytes")
        return self


class Startup(Model):
    description: str
    argv: list[str] = Field(min_length=1)
    env: dict[str, str | None] = Field(default_factory=dict)
    required_files: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    input_target: Literal["tool_stdin", "tool_control"] = "tool_stdin"
    input_notes: str


class BoardProfile(Model):
    schema_version: Literal[1]
    name: str = Field(min_length=1)
    description: str
    connector: str
    connection: Annotated[SerialConnection | SSHConnection, Field(discriminator="transport")]
    forwarding: Forwarding
    notes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    startups: dict[str, Startup] = Field(default_factory=dict)


class Profiles:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()

    def read(self, profile_id: str) -> dict:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", profile_id):
            raise ValueError("profile_id must be a lowercase file stem, not a path")
        path = (self.directory / f"{profile_id}.json").resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("profile must stay inside the profiles directory")
        # Re-read on each request: adding/editing a profile needs no server restart.
        profile = BoardProfile.model_validate_json(path.read_text(encoding="utf-8"))
        return {"profile_id": profile_id, **profile.model_dump(exclude_none=True)}

    def list(self) -> dict:
        entries, errors = [], []
        for path in sorted(self.directory.glob("*.json")):
            try:
                profile = self.read(path.stem)
                entries.append({key: profile[key] for key in
                                ("profile_id", "name", "description", "connector", "connection")})
                entries[-1]["startups"] = list(profile["startups"])
            except (ValueError, LookupError, OSError) as exc:
                errors.append({"profile_id": path.stem, "error_type": type(exc).__name__})
        return {"status": "ok", "profiles": entries, "errors": errors}

    def prepare_startup(self, profile_id: str, startup: str, project_dir: str | None,
                        port: str | None, parameters: dict[str, str]) -> dict:
        profile = self.read(profile_id)
        recipe = profile["startups"].get(startup)
        if recipe is None:
            return {"status": "unknown_startup", "available": list(profile["startups"])}
        if recipe["platforms"] and sys.platform not in recipe["platforms"]:
            return {"status": "unsupported_platform", "platform": sys.platform, "supported": recipe["platforms"]}
        values = dict(parameters)
        values.pop("port", None)
        values.pop("project_dir", None)
        if project_dir:
            project = Path(project_dir).expanduser()
            if not project.is_absolute() or not project.is_dir():
                raise ValueError("project_dir must be an absolute existing project directory")
            values["project_dir"] = str(project.resolve())
        if port:
            values["port"] = port
        if "baudrate" not in values and profile["connection"].get("baudrate"):
            values["baudrate"] = str(profile["connection"]["baudrate"])
        templates = recipe["argv"] + [v for v in recipe["env"].values() if v is not None] + recipe["required_files"]
        fields = {"project_dir"}
        for template in templates:
            for _, field, spec, conversion in string.Formatter().parse(template):
                if field is not None:
                    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", field) or spec or conversion:
                        raise ValueError("startup templates support only simple {parameter} placeholders")
                    fields.add(field)
        missing = sorted(name for name in fields if not values.get(name))
        if missing:
            return {"status": "needs_input", "missing_fields": missing,
                    "questions": ["请补充已无法从项目材料确认的启动参数：" + "、".join(missing)],
                    "next_action": "Resolve from verified project settings first; ask only remaining questions. Nothing was started."}
        rendered = lambda value: value.format_map(values)
        directory = Path(values["project_dir"])
        required = [(directory / rendered(path)).resolve() for path in recipe["required_files"]]
        absent = [str(path) for path in required if not path.is_file()]
        if absent:
            return {"status": "missing_prerequisites", "missing_files": absent,
                    "next_action": "Inspect project/startup configuration; no build, install or device operation was performed."}
        return {"status": "ready", "next_tool": "process_open",
                "open_args": {"argv": [rendered(arg) for arg in recipe["argv"]],
                              "cwd": str(directory), "env": {k: rendered(v) if v is not None else None for k, v in recipe["env"].items()},
                              "encoding": profile["forwarding"]["encoding"], "resource_port": port,
                              "input_target": recipe["input_target"], "required_files": [str(p) for p in required]},
                "input_notes": recipe["input_notes"], "required_files": [str(p) for p in required],
                "device_ready": "unknown", "note": "Static preparation only; read tool startup output after opening."}
