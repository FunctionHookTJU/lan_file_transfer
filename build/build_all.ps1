param(
    [string]$Version = "1.9.3"
)

$ErrorActionPreference = "Stop"

$scriptDir = $PSScriptRoot
$projectRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
Set-Location $projectRoot

Write-Host "==> Build EXE (v$Version)"
& (Join-Path $scriptDir "build_exe.ps1") -KeepBaseExe -Version $Version
if ($LASTEXITCODE -ne 0) {
    throw "EXE build failed with exit code $LASTEXITCODE"
}

Write-Host "Build finished:"
Write-Host "  dist\LANFileTransfer.exe"
Write-Host "  dist\LANFileTransfer-v$Version.exe"
