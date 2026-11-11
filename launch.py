"""Start the local stdio MCP server; protocol output is reserved for the SDK."""

import argparse
import hashlib
import logging
import os
from pathlib import Path

from board_console.server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Board Console MCP — serial, SSH and offline replay")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(),
                        help="Project context; defaults to the working directory supplied by the MCP host")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--data-dir", type=Path, help="Explicit recording directory (legacy/manual use)")
    storage.add_argument("--state-root", type=Path, help="User-level state root; partitioned by workspace path")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error("workspace directory does not exist")
    if args.state_root:
        identity = os.path.normcase(str(workspace))
        project_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        data_dir = args.state_root.resolve() / "projects" / project_key
    else:
        data_dir = args.data_dir or workspace / ".board-console"
    build_server(workspace, data_dir).run(transport="stdio")


if __name__ == "__main__":
    main()
