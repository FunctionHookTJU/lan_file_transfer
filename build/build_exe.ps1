param(
    [string]$PythonExe = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

Write-Host "Using Python: $PythonExe"
& $PythonExe -m pip install -r requirements.txt pyinstaller

$distDir = Join-Path $projectRoot "dist"
$buildDir = Join-Path $projectRoot "build\pyinstaller"

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "LANFileTransfer" `
    --distpath $distDir `
    --workpath $buildDir `
    --add-data "templates;templates" `
    tray_app.py

Write-Host "EXE build finished: $distDir\LANFileTransfer.exe"
