param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\BoardConsole'),
    [switch]$ReplaceExisting
)
$ErrorActionPreference = 'Stop'
$bundleRoot = $PSScriptRoot
$manifestPath = Join-Path $bundleRoot 'release.json'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw 'This entry requires a built release directory. For source development use setup.ps1.'
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.product -ne 'board-console' -or $manifest.version -notmatch '^\d+\.\d+\.\d+$') {
    throw 'Invalid release manifest.'
}
$targetRoot = [IO.Path]::GetFullPath((Join-Path $InstallRoot ("versions\" + $manifest.version)))
$targetPrefix = $targetRoot.TrimEnd('\') + '\'
$sourcePrefix = [IO.Path]::GetFullPath($bundleRoot).TrimEnd('\') + '\'
$copies = @()
foreach ($file in $manifest.files) {
    $source = [IO.Path]::GetFullPath((Join-Path $bundleRoot $file.path))
    $target = [IO.Path]::GetFullPath((Join-Path $targetRoot $file.path))
    if (-not $source.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not $target.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Manifest path escapes its release directory.'
    }
    if ($file.sha256 -notmatch '^[a-f0-9]{64}$' -or
        (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) {
        throw "Release file checksum mismatch: $($file.path)"
    }
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) {
            throw "Existing version differs: $target. Publish a new version instead of overwriting running files."
        }
    } else {
        $copies += @{ Source = $source; Target = $target; Hash = $file.sha256 }
    }
}
if (-not ($manifest.files | Where-Object { $_.path -eq 'board-console.exe' })) {
    throw 'Manifest does not contain board-console.exe.'
}
foreach ($item in $copies) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $item.Target) -Force | Out-Null
    Copy-Item -LiteralPath $item.Source -Destination $item.Target
    if ((Get-FileHash -LiteralPath $item.Target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.Hash) {
        throw "Installed file checksum mismatch: $($item.Target)"
    }
}
$entry = Join-Path $targetRoot 'board-console.exe'
$configureArgs = @('configure')
if ($ReplaceExisting) { $configureArgs += '--replace-existing' }
& $entry @configureArgs
if ($LASTEXITCODE -ne 0) { throw 'Files installed, but MCP registration failed. Existing unrelated settings were preserved.' }
Write-Output "Installed: $targetRoot"
Write-Output 'No Python installation, PATH changes or background service required. Existing Agent sessions keep their current process.'
