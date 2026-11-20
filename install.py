"""Cross-platform source installation; never run during normal MCP startup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true", help="Register in Codex user configuration")
    parser.add_argument("--skip-dependencies", action="store_true", help="Reuse an already installed environment")
    parser.add_argument("--replace-existing", action="store_true", help="With --codex, explicitly replace another installation")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required")
    if args.replace_existing and not args.codex:
        parser.error("--replace-existing requires --codex")
    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    # Do not resolve this executable: POSIX venv Python is commonly a symlink.
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        if args.skip_dependencies:
            parser.error("No installed environment; omit --skip-dependencies")
        if environment.exists():
            parser.error("Existing .venv is incomplete; inspect it before reinstalling")
        venv.EnvBuilder(with_pip=True).create(environment)
    if not args.skip_dependencies:
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "requirements.txt"),
                        "--disable-pip-version-check"], cwd=root, check=True, stdout=sys.stderr)
    if args.codex:
        command = [str(python), str(root / "launch.py"), "configure"]
        if args.replace_existing:
            command.append("--replace-existing")
        subprocess.run(command, cwd=root, check=True)
        return
    # Generic mode prints configuration only; it does not change any Agent settings.
    entry = {"command": str(python), "args": [str(root / "launch.py"),
             "--state-root", str(Path.home() / ".board-console")]}
    print(json.dumps({"mcpServers": {"board_console": entry}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
