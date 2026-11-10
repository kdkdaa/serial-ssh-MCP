"""Start the local stdio MCP server; protocol output is reserved for the SDK."""

import argparse
import logging
from pathlib import Path

from board_console.server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Board Console MCP — serial, SSH and offline replay")
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error("workspace directory does not exist")
    build_server(workspace, args.data_dir or workspace / ".board-console").run(transport="stdio")


if __name__ == "__main__":
    main()
