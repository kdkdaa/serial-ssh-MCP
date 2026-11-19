"""One Agent-owned stdio process; no background service or connection sharing."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from pathlib import Path

from . import __version__


def main() -> None:
    # Dispatch worker/version/configuration before importing MCP and SSH libraries.
    if len(sys.argv) > 1 and sys.argv[1] == "--process-worker":
        from .process_worker import main as worker_main
        raise SystemExit(worker_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "configure":
        from .configure import main as configure_main
        configure_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="Board Console: Agent-owned serial, SSH and tool sessions")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--profiles-dir", type=Path, help="User board files; same-name files override bundled defaults")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--data-dir", type=Path, help="Project data root; each launch gets its own instance directory")
    storage.add_argument("--state-root", type=Path, help="User data root, partitioned by workspace and instance")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error("workspace directory does not exist")
    if args.state_root:
        project_key = hashlib.sha256(os.path.normcase(str(workspace)).encode("utf-8")).hexdigest()
        data_dir = args.state_root.resolve() / "projects" / project_key
    else:
        data_dir = args.data_dir or workspace / ".board-console"
    from .server import build_server
    profiles_dir = args.profiles_dir or (args.state_root / "boards" if args.state_root else None)
    build_server(workspace, data_dir, profiles_dir).run(transport="stdio")
