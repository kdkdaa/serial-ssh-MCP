"""Register a source or bundled stdio entry without changing user preferences."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

from .runtime import frozen, install_root, resource_root, server_command


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return stream.read()


def replace_file(path: Path, before: str, after: str) -> None:
    if before == after:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        if read_text(path) != before:
            raise SystemExit(f"Configuration changed concurrently; rerun installation: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def same_path(left: str, right: str) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def owned(entry: dict, command: list[str]) -> bool:
    if "url" in entry:
        return False
    if len(command) == 1:
        return isinstance(entry.get("command"), str) and same_path(entry["command"], command[0])
    args = entry.get("args", [])
    return bool(args and isinstance(args[0], str) and same_path(args[0], command[1]))


def desired_entry(existing: dict | None, command: list[str], state_root: Path,
                  replace_existing: bool, defaults: bool) -> dict:
    existing = existing or {}
    if existing and not owned(existing, command) and not replace_existing:
        raise SystemExit("Different board_console entry preserved; use --replace-existing only to intentionally replace it.")
    if "url" in existing:
        raise SystemExit("An HTTP board_console entry exists; remove that entry explicitly before installing stdio.")
    result = dict(existing)
    previous_args = list(existing.get("args", []))
    legacy_root = resource_root()
    if previous_args and str(previous_args[0]).lower().endswith("launch.py"):
        legacy_root = Path(previous_args.pop(0)).resolve().parent
    is_legacy = (len(previous_args) == 2 and previous_args[0] == "--workspace" and
                 same_path(previous_args[1], str(legacy_root)))
    if is_legacy:
        previous_args = []
        if isinstance(result.get("cwd"), str) and same_path(result["cwd"], str(legacy_root)):
            result.pop("cwd")
    result["command"] = command[0]
    result["args"] = command[1:] + (previous_args or ["--state-root", str(state_root)])
    if defaults:
        result.setdefault("startup_timeout_sec", 20)
        result.setdefault("tool_timeout_sec", 60)
    return result


def update_toml(text: str, entry: dict) -> str:
    original = tomllib.loads(text)
    old = original.get("mcp_servers", {}).get("board_console")
    newline = "\r\n" if "\r\n" in text else "\n"
    if text and not text.endswith("\n"):
        text += newline
    if old is None:
        lines = ["", "[mcp_servers.board_console]"]
        lines += [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in entry.items()]
        result = text + newline.join(lines) + newline
    else:
        header = re.search(r"(?m)^\[mcp_servers\.board_console\][ \t]*(?:#[^\r\n]*)?\r?\n", text + newline)
        if header is None:
            raise SystemExit("Nonstandard board_console table preserved; use a plain [mcp_servers.board_console] table.")
        start = min(header.end(), len(text))
        following = re.search(r"(?m)^\s*\[", text[start:])
        end = start + following.start() if following else len(text)
        body = text[start:end]
        for key in sorted(set(old) | set(entry)):
            if key in old and key in entry and old[key] == entry[key]:
                continue
            match = re.search(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=", body)
            replacement = f"{key} = {json.dumps(entry[key], ensure_ascii=False)}{newline}" if key in entry else ""
            if match:
                value_end = match.end()
                while True:
                    boundary = body.find("\n", value_end)
                    value_end = len(body) if boundary < 0 else boundary + 1
                    try:
                        parsed = tomllib.loads(body[match.start():value_end])
                    except tomllib.TOMLDecodeError:
                        if value_end == len(body):
                            raise SystemExit(f"Could not safely update {key}; configuration preserved.")
                        continue
                    if parsed != {key: old[key]}:
                        raise SystemExit(f"Ambiguous {key} assignment; configuration preserved.")
                    break
                body = body[:match.start()] + replacement + body[value_end:]
            elif key in old:
                raise SystemExit(f"Nonstandard {key} assignment preserved; no configuration written.")
            else:
                body += ("" if not body or body.endswith("\n") else newline) + replacement
        result = text[:start] + body + text[end:]
    expected = copy.deepcopy(original)
    expected.setdefault("mcp_servers", {})["board_console"] = entry
    if tomllib.loads(result) != expected:
        raise SystemExit("Unrelated TOML values would change; configuration preserved.")
    return result


def configure(replace_existing: bool = False, check: bool = False) -> None:
    root = install_root()
    codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    state_root = codex_dir / "board-console"
    command = server_command()
    config_path = codex_dir / "config.toml"
    original = read_text(config_path)
    existing = tomllib.loads(original).get("mcp_servers", {}).get("board_console")
    receipt_path = codex_dir / "board-console-install.json"
    receipt_original = read_text(receipt_path)
    receipt = json.loads(receipt_original) if receipt_original else {}
    previous_install = bool(existing and receipt.get("product") == "board-console" and
                            isinstance(existing.get("command"), str) and
                            isinstance(receipt.get("command"), str) and
                            same_path(existing["command"], receipt["command"]) and
                            existing.get("args", []) == receipt.get("args"))
    entry = desired_entry(existing, command, state_root, replace_existing or previous_install, defaults=True)
    updated = update_toml(original, entry)
    mcp_path = root / ".mcp.json"
    mcp_original = read_text(mcp_path)
    mcp_config = json.loads(mcp_original) if mcp_original else {"mcpServers": {}}
    prior = mcp_config.setdefault("mcpServers", {}).get("board_console")
    mcp_config["mcpServers"]["board_console"] = desired_entry(prior, command, state_root, replace_existing, defaults=False)
    mcp_updated = json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n"
    receipt_updated = json.dumps({"product": "board-console", "command": command[0], "args": entry["args"]}, indent=2) + "\n"
    changes = [(config_path, original, updated), (mcp_path, mcp_original, mcp_updated),
               (receipt_path, receipt_original, receipt_updated)]
    if not frozen():
        project_path = resource_root() / ".codex" / "config.toml"
        project_original = read_text(project_path)
        parsed = tomllib.loads(project_original)
        override = parsed.get("mcp_servers", {}).get("board_console")
        if override is not None:
            if not owned(override, command):
                raise SystemExit(f"Different project override preserved: {project_path}")
            if set(override) - {"command", "args", "cwd", "startup_timeout_sec", "tool_timeout_sec"}:
                raise SystemExit(f"Customized project override preserved: {project_path}")
            header = re.search(r"(?m)^\[mcp_servers\.board_console\][ \t]*\r?\n", project_original)
            if header is None:
                raise SystemExit(f"Nonstandard project override preserved: {project_path}")
            following = re.search(r"(?m)^\s*\[", project_original[header.end():])
            end = header.end() + following.start() if following else len(project_original)
            project_updated = project_original[:header.start()] + project_original[end:]
            expected = copy.deepcopy(parsed)
            expected["mcp_servers"].pop("board_console")
            actual = tomllib.loads(project_updated)
            for value in (expected, actual):
                if value.get("mcp_servers") == {}:
                    value.pop("mcp_servers")
            if expected != actual:
                raise SystemExit("Could not safely remove legacy project override.")
            changes.append((project_path, project_original, project_updated))
    for path, before, _ in changes:
        if read_text(path) != before:
            raise SystemExit(f"Configuration changed concurrently: {path}")
    if check:
        print(json.dumps({"status": "configuration_valid", "mode": "check_only", "writes": 0,
                          "would_change": [str(p) for p, before, after in changes if before != after]},
                         ensure_ascii=False))
        return
    for path, before, after in changes:
        replace_file(path, before, after)
    print(f"Configured user MCP: {config_path}")
    print("Existing enabled/tool/environment preferences preserved. No environment variables changed.")
    print("Agent starts/stops this stdio process; no background service installed.")


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace-existing", action="store_true", help="Explicitly migrate a differently located stdio installation")
    parser.add_argument("--check", action="store_true", help="Validate registration changes without writing files or starting MCP")
    args = parser.parse_args(argv)
    configure(args.replace_existing, args.check)
