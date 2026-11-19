param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
Push-Location -LiteralPath $projectRoot
try {
    # Maintainer-only command: dependencies and PyInstaller must already be installed.
    & $Python -m PyInstaller --noconfirm --distpath (Join-Path $projectRoot 'dist') --workpath (Join-Path $projectRoot 'build') (Join-Path $projectRoot 'board-console.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Release build failed.' }
    $bundleRoot = (Resolve-Path -LiteralPath (Join-Path $projectRoot 'dist\board-console')).Path
    $version = (& (Join-Path $bundleRoot 'board-console.exe') --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Could not read bundle version.' }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'install.ps1') -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $projectRoot 'INSTALL.md') -Destination $bundleRoot
    $files = @(Get-ChildItem -LiteralPath $bundleRoot -File -Recurse -Force | Where-Object {
        $_.FullName -ne (Join-Path $bundleRoot 'release.json')
    } | ForEach-Object {
        [ordered]@{ path = $_.FullName.Substring($bundleRoot.Length + 1).Replace('\', '/');
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
    })
    $manifest = [ordered]@{ product = 'board-console'; version = $version; files = $files }
    [IO.File]::WriteAllText((Join-Path $bundleRoot 'release.json'),
        ($manifest | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    Write-Output "Release directory: $bundleRoot"
} finally {
    Pop-Location
}
