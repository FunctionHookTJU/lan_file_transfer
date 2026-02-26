param(
    [string]$PythonExe = ".venv\Scripts\python.exe",
    [switch]$SkipInstallDeps
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

Write-Host "Using Python: $PythonExe"
if (-not $SkipInstallDeps) {
    & $PythonExe -m pip install -r requirements.txt pyinstaller
}

$distDir = Join-Path $projectRoot "dist"
$buildDir = Join-Path $projectRoot "build\pyinstaller"
$specFile = Join-Path $projectRoot "LANFileTransfer.spec"

if (-not (Test-Path $specFile)) {
    throw "Missing spec file: $specFile"
}

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $distDir `
    --workpath $buildDir `
    $specFile

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "EXE build finished: $distDir\LANFileTransfer.exe"
