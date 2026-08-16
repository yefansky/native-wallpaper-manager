@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==========================================
echo   Desktop Gallery - Setup
echo ==========================================

call :find_python
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer was not found.
    pause
    exit /b 1
)

echo [1/3] Installing dependencies...
"%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo [ERROR] Package installation failed.
    pause
    exit /b 1
)

echo [2/3] Creating Start menu and desktop shortcuts...
set "NWM_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$root=$env:NWM_ROOT; $shell=New-Object -ComObject WScript.Shell; $name=(-join [char[]](0x684c,0x9762,0x753b,0x5eca))+'.lnk'; $links=@((Join-Path ([Environment]::GetFolderPath('Programs')) $name),(Join-Path ([Environment]::GetFolderPath('Desktop')) $name)); foreach($link in $links){ $s=$shell.CreateShortcut($link); $s.TargetPath=Join-Path $root 'run.bat'; $s.WorkingDirectory=$root; $icon=Join-Path $root 'app\icon.ico'; if(Test-Path -LiteralPath $icon){$s.IconLocation=$icon+',0'}; $s.Save() }"
if errorlevel 1 (
    echo [ERROR] Shortcut creation failed.
    pause
    exit /b 1
)

echo [3/3] Enabling launch at sign-in...
"%PYTHON_EXE%" -c "import sys; sys.path.insert(0, r'%~dp0app'); from product import set_autostart; raise SystemExit(0 if set_autostart(True) else 1)"
if errorlevel 1 (
    echo [ERROR] Launch-at-sign-in setup failed.
    pause
    exit /b 1
)

echo.
echo Setup completed. Run run.bat or use the Desktop Gallery shortcut.
echo App data is stored in %LOCALAPPDATA%\NativeWallpaperManager.
pause
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
