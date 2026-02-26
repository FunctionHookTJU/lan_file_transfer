param(
    [string]$PythonExe = ".venv\Scripts\python.exe",
    [string]$IsccPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    [string]$Version = "1.0.0",
    [switch]$SkipInstallDeps
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$buildExeScript = Join-Path $PSScriptRoot "build_exe.ps1"
$buildInstallerScript = Join-Path $PSScriptRoot "build_installer.ps1"

if (-not (Test-Path $buildExeScript)) {
    throw "Missing script: $buildExeScript"
}
if (-not (Test-Path $buildInstallerScript)) {
    throw "Missing script: $buildInstallerScript"
}

Write-Host "[1/2] Building EXE..."
& $buildExeScript -PythonExe $PythonExe -SkipInstallDeps:$SkipInstallDeps

Write-Host "[2/2] Building installer..."
& $buildInstallerScript -IsccPath $IsccPath -Version $Version

Write-Host "All done."
Write-Host "EXE: $projectRoot\dist\LANFileTransfer.exe"
Write-Host "Setup: $projectRoot\dist\LANFileTransfer-v$Version-Setup.exe"
