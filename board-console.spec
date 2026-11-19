"""Windows onedir release; build explicitly, never during Agent startup."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH)
analysis = Analysis(
    [str(root / "launch.py")], pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "boards"), "boards"), (str(root / "docs"), "docs"),
           (str(root / ".agents"), ".agents")],
    hiddenimports=collect_submodules("mcp") + ["board_console.process_worker", "board_console.configure"],
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True,
                 name="board-console", debug=False, strip=False, upx=False, console=True)
bundle = COLLECT(executable, analysis.binaries, analysis.datas, strip=False,
                 upx=False, name="board-console")
