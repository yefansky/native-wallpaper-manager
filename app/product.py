"""产品化支撑：开机自启注册、单实例锁、自动启动命令。

- 开机自启：写 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`，
  指向 `pythonw.exe app\\native_wallpaper.py`（无控制台窗口）。
- 单实例：命名互斥体防止重复启动；重复时只提示一次并退出。
"""
from __future__ import annotations

import ctypes
import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "NativeWallpaperManager"
MUTEX_NAME = "Local\\NativeWallpaperManagerSingleInstance"


def _pythonw() -> str:
    executable = sys.executable
    if executable.lower().endswith("python.exe"):
        executable = executable[: -len("python.exe")] + "pythonw.exe"
    return executable


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = str(Path(__file__).resolve().parents[1] / "app" / "native_wallpaper.py")
    return f'"{_pythonw()}" "{script}"'


def autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def notify_already_running() -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, "桌面画廊已在运行，请在系统托盘查看。", "桌面画廊", 0x40)
    except Exception:
        pass


class SingleInstance:
    def __init__(self, name: str = MUTEX_NAME):
        ctypes.windll.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
        ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        self.already_running = ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
