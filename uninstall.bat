@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   桌面画廊 · 卸载
echo ==========================================

rem 关闭开机自启
set "PYDIR="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYDIR set "PYDIR=%%~dpi"
if defined PYDIR (
    "%PYDIR%python.exe" -c "import sys; sys.path.insert(0, r'%~dp0app'); from product import set_autostart; set_autostart(False); print('已关闭开机自启')" 2>nul
)

rem 移除快捷方式
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\桌面画廊.lnk" 2>nul
del "%USERPROFILE%\Desktop\桌面画廊.lnk" 2>nul
echo 已移除开始菜单与桌面快捷方式。

echo.
echo 说明：
echo  - 数据目录 %LOCALAPPDATA%\NativeWallpaperManager 已保留（含本地图库与加密会话）。
echo  - 如需彻底删除数据，请手动删除上述目录。
echo  - 如需卸载 Python 依赖，请手动执行：
echo      python -m pip uninstall -y -r requirements.txt
pause
