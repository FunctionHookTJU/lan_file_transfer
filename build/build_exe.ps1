param(
    [string]$PythonExe = ".venv\Scripts\python.exe",
    [string]$Version = "",
    [switch]$KeepBaseExe,
    [switch]$SkipInstallDeps,
    [switch]$DebugBuild
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

Write-Host "Using Python: $PythonExe"
$buildMode = if ($DebugBuild) { "Debug" } else { "Release" }
Write-Host "Build mode: $buildMode"
if (-not $SkipInstallDeps) {
    & $PythonExe -m pip install -r requirements.txt pyinstaller
}

$distDir = Join-Path $projectRoot "dist"
$buildDir = Join-Path $projectRoot "build\pyinstaller"
$specFile = Join-Path $projectRoot "LANFileTransfer.spec"

if (-not (Test-Path $specFile)) {
    throw "Missing spec file: $specFile"
}

$previousPyiDebug = $env:LANFILETRANSFER_PYI_DEBUG
$env:LANFILETRANSFER_PYI_DEBUG = if ($DebugBuild) { "1" } else { "0" }

try {
    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $distDir `
        --workpath $buildDir `
        $specFile
}
finally {
    if ($null -eq $previousPyiDebug) {
        Remove-Item Env:LANFILETRANSFER_PYI_DEBUG -ErrorAction SilentlyContinue
    } else {
        $env:LANFILETRANSFER_PYI_DEBUG = $previousPyiDebug
    }
}

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "EXE build finished: $distDir\LANFileTransfer.exe"

if ($Version) {
    $versionedExe = Join-Path $distDir "LANFileTransfer-v$Version.exe"
    $baseExe = Join-Path $distDir "LANFileTransfer.exe"
    if ($KeepBaseExe) {
        Copy-Item -Path $baseExe -Destination $versionedExe -Force
        Write-Host "Versioned EXE: $versionedExe"
    } else {
        Move-Item -Path $baseExe -Destination $versionedExe -Force
        Write-Host "EXE renamed to versioned file: $versionedExe"
    }
}
