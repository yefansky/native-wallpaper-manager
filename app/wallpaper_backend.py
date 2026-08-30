"""Windows 原生壁纸适配器。

这个模块只负责向 Windows 提交壁纸请求并观察系统状态，
不知道图库、联网下载或 Tk 按钮。
"""
from __future__ import annotations

import ctypes
import os
import time
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


SPI_SETDESKWALLPAPER = 0x0014
SPI_GETDESKWALLPAPER = 0x0073
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
SUPPORTED_IMAGE_EXTENSIONS = {".bmp", ".jpg", ".jpeg", ".png"}
WALLPAPER_BUFFER_CHARS = 32768


@dataclass(frozen=True)
class WallpaperApplyResult:
    """Windows 接受设置请求后的有界观察结果。

    ``confirmed`` 为 False 不等于 API 失败：Windows 可能已接受请求，
    但在确认时间窗口内还没有对外暴露新路径，或被其他壁纸软件覆盖。
    """

    requested_path: Path
    confirmed: bool
    observed_path: Path | None
    attempts: int


def same_windows_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _system_parameters_info(action: int, ui_param: int, value, flags: int) -> bool:
    return bool(ctypes.windll.user32.SystemParametersInfoW(action, ui_param, value, flags))


def _current_wallpaper_from_system_api() -> Path | None:
    buffer = ctypes.create_unicode_buffer(WALLPAPER_BUFFER_CHARS)
    if _system_parameters_info(SPI_GETDESKWALLPAPER, len(buffer), buffer, 0) and buffer.value:
        return Path(buffer.value)
    return None


def _current_wallpaper_from_registry() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
            value, _kind = winreg.QueryValueEx(key, "WallPaper")
    except OSError:
        return None
    return Path(value) if value else None


def current_native_wallpaper() -> Path:
    """读取 Windows 公开系统接口所观察到的当前壁纸。

    SPI_GETDESKWALLPAPER 是主来源；注册表只是兼容性回退，不再被当成
    设置成功的唯一同步真相。
    """

    observed = _current_wallpaper_from_system_api() or _current_wallpaper_from_registry()
    if observed is None:
        raise OSError("Windows 未返回当前壁纸路径")
    return observed


def set_native_wallpaper(
    path: Path,
    *,
    confirm_timeout: float = 2.0,
    confirm_interval: float = 0.1,
    current_reader: Callable[[], Path] | None = None,
    system_parameters_info: Callable[[int, int, object, int], bool] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> WallpaperApplyResult:
    """提交壁纸请求，并在有界时间内轮询 Windows 公开读取接口。

    只有设置 API 返回失败才抛异常。API 已返回成功但状态尚未收敛时，
    返回 ``confirmed=False``，由上层以“已接受、待确认”呈现，不删除资产。
    """

    requested = path.resolve()
    if requested.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise ValueError("Windows 原生壁纸仅支持 BMP/JPG/PNG 图片")
    if not requested.is_file() or requested.stat().st_size == 0:
        raise ValueError("壁纸文件不存在或为空")

    setter = system_parameters_info or _system_parameters_info
    flags = SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    if not setter(SPI_SETDESKWALLPAPER, 0, str(requested), flags):
        raise ctypes.WinError()

    reader = current_reader or current_native_wallpaper
    clock = monotonic or time.monotonic
    sleep = sleeper or time.sleep
    deadline = clock() + max(0.0, confirm_timeout)
    attempts = 0
    observed: Path | None = None
    while True:
        attempts += 1
        try:
            observed = reader()
        except OSError:
            observed = None
        if observed is not None and same_windows_path(observed, requested):
            return WallpaperApplyResult(requested, True, observed, attempts)
        now = clock()
        if now >= deadline:
            return WallpaperApplyResult(requested, False, observed, attempts)
        sleep(min(max(0.0, confirm_interval), max(0.0, deadline - now)))
