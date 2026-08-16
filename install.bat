@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   桌面画廊 · 安装向导
echo ==========================================

set "PYDIR="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYDIR set "PYDIR=%%~dpi"
if not defined PYDIR (
    echo [错误] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH。
    pause & exit /b 1
)

echo [1/3] 安装依赖...
"%PYDIR%python.exe" -m pip install -r requirements.txt
if errorlevel 1 ( echo [错误] 依赖安装失败，请检查网络。 & pause & exit /b 1 )

echo [2/3] 创建开始菜单与桌面快捷方式...
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\桌面画廊.lnk"
set "DESKTOP=%USERPROFILE%\Desktop\桌面画廊.lnk"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%STARTMENU%'); $s.TargetPath='%~dp0run.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0app\icon.ico,0'; $s.Save()"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%DESKTOP%'); $s.TargetPath='%~dp0run.bat'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0app\icon.ico,0'; $s.Save()"

echo [3/3] 开启开机自启...
"%PYDIR%python.exe" -c "import sys; sys.path.insert(0, r'%~dp0app'); from product import set_autostart; print('开机自启已开启' if set_autostart(True) else '开机自启设置失败')"

echo.
echo 安装完成！已创建开始菜单/桌面快捷方式，并开启开机自启。
echo 双击桌面"桌面画廊"图标或运行 run.bat 启动。
echo 数据保存在 %LOCALAPPDATA%\NativeWallpaperManager。
pause
