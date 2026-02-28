@echo off
setlocal

set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build_exe.ps1" %*
if errorlevel 1 (
  echo EXE build failed.
  exit /b 1
)

echo EXE build finished successfully.
exit /b 0
