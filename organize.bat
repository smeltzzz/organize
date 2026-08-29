@echo off
REM ==============================================================================
REM Organize — Jellyfin Media Management Launcher for Windows (cmd.exe)
REM ==============================================================================
setlocal

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    py -3 "%~dp0organize.py" %*
    exit /b %ERRORLEVEL%
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    python "%~dp0organize.py" %*
    exit /b %ERRORLEVEL%
)

echo [ERROR] Python 3.11+ is required but was not found.
echo Please install Python 3.11+ from https://www.python.org/downloads/
echo Ensure 'Add python.exe to PATH' is checked during setup.
exit /b 1
