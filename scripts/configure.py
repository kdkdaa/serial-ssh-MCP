"""Register Board Console once for the Codex user, preserving unrelated settings."""

import json
import os
import re
import sys
import tomllib
from pathlib import Path


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return stream.read()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def without_board_table(text: str) -> str:
    """Only remove the generated plain table, and prove other TOML values survived."""
    original = tomllib.loads(text)
    header = re.search(r"(?m)^\[mcp_servers\.board_console\][ \t]*(?:\r?\n|$)", text)
    if header is None:
        raise SystemExit("Nonstandard board_console TOML table; preserved it for manual review.")
    following = re.search(r"(?m)^\s*\[", text[header.end():])
    end = header.end() + following.start() if following else len(text)
    updated = text[:header.start()] + text[end:]
    expected = original
    expected["mcp_servers"].pop("board_console")
    actual = tomllib.loads(updated)
    for value in (expected, actual):
        if value.get("mcp_servers") == {}:
            value.pop("mcp_servers")
    if expected != actual:
        raise SystemExit("Cannot preserve unrelated TOML entries; no configuration written.")
    return updated


def configure() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    codex_dir = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    command = Path(sys.executable).resolve().as_posix()
    # No fixed cwd/workspace: the host supplies the calling project's working directory.
    args = [str(root / "launch.py"), "--state-root", str(codex_dir / "board-console")]
    entry = {"command": command, "args": args, "startup_timeout_sec": 20, "tool_timeout_sec": 60}
    legacy = {"command": command, "args": [str(root / "launch.py"), "--workspace", str(root)],
              "cwd": str(root), "startup_timeout_sec": 20, "tool_timeout_sec": 60}

    config_path = codex_dir / "config.toml"
    original = read_text(config_path)
    existing = tomllib.loads(original).get("mcp_servers", {}).get("board_console")
    updated = original
    if existing is not None and existing != entry:
        if existing != legacy:
            raise SystemExit(f"Different user board_console entry preserved: {config_path}")
        updated = without_board_table(updated)
    if existing != entry:
        lines = ["", "[mcp_servers.board_console]"]
        lines.extend(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in entry.items())
        updated += "\n".join(lines) + "\n"
    if tomllib.loads(updated).get("mcp_servers", {}).get("board_console") != entry:
        raise SystemExit("Generated user configuration is invalid; no configuration written.")

    # Remove this install's old project override so it cannot shadow user registration.
    project_path = root / ".codex" / "config.toml"
    project_original = read_text(project_path)
    project_entry = tomllib.loads(project_original).get("mcp_servers", {}).get("board_console")
    project_updated = project_original
    if project_entry is not None:
        if project_entry not in (legacy, entry):
            raise SystemExit(f"Different project board_console override preserved: {project_path}")
        project_updated = without_board_table(project_original)

    mcp_path = root / ".mcp.json"
    mcp_original = read_text(mcp_path)
    mcp_config = json.loads(mcp_original) if mcp_original else {"mcpServers": {}}
    mcp_entry = {"command": command, "args": args}
    legacy_mcp = {key: legacy[key] for key in ("command", "args", "cwd")}
    existing_mcp = mcp_config.setdefault("mcpServers", {}).get("board_console")
    if existing_mcp is not None and existing_mcp not in (legacy_mcp, mcp_entry):
        raise SystemExit("Different .mcp.json board_console entry preserved; no configuration written.")
    mcp_config["mcpServers"]["board_console"] = mcp_entry
    mcp_updated = json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n"

    # Check all files before writing; preserve an edit detected during preparation.
    changes = [(config_path, original, updated), (project_path, project_original, project_updated),
               (mcp_path, mcp_original, mcp_updated)]
    for path, before, after in changes:
        if read_text(path) != before:
            raise SystemExit(f"Configuration changed concurrently; rerun setup: {path}")
    for path, before, after in changes:
        if before != after:
            write_text(path, after)
    print(f"Configured user MCP for all projects: {config_path}")
    if project_original != project_updated:
        print(f"Removed this install's old project override: {project_path}")
    print("No environment variables changed. No hardware was opened.")
    print("Reload Board Console in Codex; then call server_status from the target project.")


if __name__ == "__main__":
    configure()
