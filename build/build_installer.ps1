param(
    [string]$IsccPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$issFile = Join-Path $projectRoot "installer\lan_file_transfer.iss"
$exeFile = Join-Path $projectRoot "dist\LANFileTransfer.exe"

if (-not (Test-Path $exeFile)) {
    throw "Missing EXE: $exeFile. Run build\build_exe.ps1 first."
}

if (-not (Test-Path $IsccPath)) {
    throw "ISCC not found at: $IsccPath. Please install Inno Setup 6."
}

& $IsccPath $issFile
if ($LASTEXITCODE -ne 0) {
    throw "ISCC compile failed with exit code $LASTEXITCODE"
}
Write-Host "Installer build finished: dist\LANFileTransfer-Setup.exe"
