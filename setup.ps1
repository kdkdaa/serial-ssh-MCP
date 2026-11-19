param([switch]$SkipDependencies, [switch]$ReplaceExisting)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        & py -3.11 -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create Python 3.11 virtual environment.' }
    }
    if (-not $SkipDependencies) {
        & $pythonPath -m pip install --only-binary=:all: -r (Join-Path $projectRoot 'requirements.txt') --disable-pip-version-check
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }
    $configureArgs = @((Join-Path $projectRoot 'scripts\configure.py'))
    if ($ReplaceExisting) { $configureArgs += '--replace-existing' }
    & $pythonPath @configureArgs
    if ($LASTEXITCODE -ne 0) { throw 'MCP configuration failed; existing settings were preserved.' }
} finally {
    Pop-Location
}
