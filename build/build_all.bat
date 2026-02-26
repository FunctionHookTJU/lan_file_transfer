@echo off
setlocal

set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build_all.ps1" %*
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

echo Build finished successfully.
exit /b 0
