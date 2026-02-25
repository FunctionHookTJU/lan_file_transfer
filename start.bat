@echo off
setlocal
cd /d "%~dp0"

echo ======================================
echo LAN File Transfer Server (Port 5000)
echo Working Dir: %CD%
echo ======================================

echo.
if exist ".venv\Scripts\python.exe" (
    echo Using virtualenv Python: .venv\Scripts\python.exe
    ".venv\Scripts\python.exe" app.py --port 5000
) else (
    echo Using system Python: python
    python app.py --port 5000
)

echo.
echo Process exited. Press any key to close.
pause >nul
