@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==========================================
echo   Desktop Gallery - Uninstall integration
echo ==========================================

call :find_python
if not errorlevel 1 (
    "%PYTHON_EXE%" -c "import sys; sys.path.insert(0, r'%~dp0app'); from product import set_autostart; set_autostart(False)" >nul 2>nul
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$name=(-join [char[]](0x684c,0x9762,0x753b,0x5eca))+'.lnk'; $links=@((Join-Path ([Environment]::GetFolderPath('Programs')) $name),(Join-Path ([Environment]::GetFolderPath('Desktop')) $name),(Join-Path ([Environment]::GetFolderPath('Programs')) 'Desktop Gallery.lnk'),(Join-Path ([Environment]::GetFolderPath('Desktop')) 'Desktop Gallery.lnk')); Remove-Item -LiteralPath $links -Force -ErrorAction SilentlyContinue"

echo Start menu shortcut, desktop shortcut, and launch-at-sign-in were removed.
echo App data was kept at %LOCALAPPDATA%\NativeWallpaperManager.
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
