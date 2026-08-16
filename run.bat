@echo off
setlocal EnableExtensions
cd /d "%~dp0"

call :find_python
if errorlevel 1 (
    echo [Desktop Gallery] Python 3.10 or newer was not found.
    echo Install Python and enable "Add python.exe to PATH", then try again.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -c "import PIL, requests, webview, pystray" >nul 2>nul
if errorlevel 1 (
    echo [Desktop Gallery] Installing required Python packages...
    "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [Desktop Gallery] Package installation failed.
        pause
        exit /b 1
    )
)

for %%I in ("%PYTHON_EXE%") do set "PYTHONW_EXE=%%~dpIpythonw.exe"
if exist "%PYTHONW_EXE%" (
    start "" "%PYTHONW_EXE%" "%~dp0app\native_wallpaper.py"
) else (
    start "Desktop Gallery" "%PYTHON_EXE%" "%~dp0app\native_wallpaper.py"
)
exit /b 0

:find_python
set "PYTHON_EXE="
for /f "delims=" %%I in ('where.exe python 2^>nul') do call :try_python "%%I"
if not defined PYTHON_EXE for /f "usebackq delims=" %%I in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do call :try_python "%%I"
if not defined PYTHON_EXE exit /b 1
exit /b 0

:try_python
if defined PYTHON_EXE exit /b 0
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=%~1"
exit /b 0
