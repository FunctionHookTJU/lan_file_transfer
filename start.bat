@echo off
setlocal
cd /d "%~dp0"

echo ======================================
echo LAN File Transfer Desktop (Port 5000)
echo Working Dir: %CD%
echo ======================================

echo.
if exist ".venv\Scripts\pythonw.exe" (
    echo Using virtualenv Pythonw: .venv\Scripts\pythonw.exe
    start "" /B ".venv\Scripts\pythonw.exe" tray_app.py --port 5000
) else if exist ".venv\Scripts\python.exe" (
    echo Using virtualenv Python: .venv\Scripts\python.exe
    start "" /B ".venv\Scripts\python.exe" tray_app.py --port 5000
) else (
    echo Using system Pythonw: pythonw
    start "" /B pythonw tray_app.py --port 5000
)
