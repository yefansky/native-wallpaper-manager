@echo off
setlocal
cd /d "%~dp0"

rem 定位 Python（pythonw 与 python 同目录）
set "PYDIR="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYDIR set "PYDIR=%%~dpi"
if not defined PYDIR (
    echo [桌面画廊] 未找到 Python，请先安装 Python 3.10+ 并勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)

rem 首次运行自动安装依赖
"%PYDIR%python.exe" -c "import PIL, requests, webview, pystray" >nul 2>nul
if errorlevel 1 (
    echo [桌面画廊] 首次运行，正在安装依赖，请稍候...
    "%PYDIR%python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [桌面画廊] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

rem 用 pythonw 启动（无控制台窗口）；start 让本窗口立即返回
start "" "%PYDIR%pythonw.exe" "%~dp0app\native_wallpaper.py"
endlocal
