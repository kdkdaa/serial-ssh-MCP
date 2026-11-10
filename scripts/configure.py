"""Generate local MCP wiring without replacing unrelated user configuration."""

import json
import sys
import tomllib
from pathlib import Path


def configure() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    command = Path(sys.executable).resolve().as_posix()
    args = [str(root / "launch.py"), "--workspace", str(root)]
    config_path = root / ".codex" / "config.toml"
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    parsed = tomllib.loads(original)
    entry = {"command": command, "args": args, "cwd": str(root),
             "startup_timeout_sec": 20, "tool_timeout_sec": 60}
    existing = parsed.get("mcp_servers", {}).get("board_console")
    if existing is not None and existing != entry:
        raise SystemExit("Existing board_console configuration differs; preserved it. Review .codex/config.toml manually.")

    mcp_path = root / ".mcp.json"
    mcp_config = json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.exists() else {"mcpServers": {}}
    mcp_entry = {"command": command, "args": args, "cwd": str(root)}
    existing_mcp = mcp_config.setdefault("mcpServers", {}).get("board_console")
    if existing_mcp is not None and existing_mcp != mcp_entry:
        raise SystemExit("Existing .mcp.json board_console configuration differs; preserved it.")

    if existing is None:
        config_path.parent.mkdir(exist_ok=True)
        lines = ["", "[mcp_servers.board_console]"]
        for key, value in entry.items():
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        with config_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
    mcp_config["mcpServers"]["board_console"] = mcp_entry
    mcp_path.write_text(json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Configured project MCP: {config_path}")
    print("Reload this project's MCP connection or open a new chat to load tools. No hardware was opened.")


if __name__ == "__main__":
    configure()
