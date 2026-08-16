"""会话持久化：用 Windows DPAPI 加密保存登录会话，支持重启后保持登录态。

安全边界：令牌、open_id、device_id 用 DPAPI（CryptProtectData）加密后写入
`%LOCALAPPDATA%\\NativeWallpaperManager\\session.enc`，密文绑定当前 Windows 用户、
不可跨机迁移，也不进入 Git/日志/文档。解密只在内存中使用；重新校验失败即清除。
这是用户明确要求的"记住登录"，是有意放宽"会话只驻留内存"的本地持久化例外。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
from ctypes import POINTER, Structure, byref, c_ulong, c_ubyte, c_void_p
from pathlib import Path

APP_DIR = Path.home() / "AppData" / "Local" / "NativeWallpaperManager"
SESSION_PATH = APP_DIR / "session.enc"


class _DataBlob(Structure):
    _fields_ = [("cbData", c_ulong), ("pbData", POINTER(c_ubyte))]


_crypt32 = ctypes.windll.crypt32
_crypt32.CryptProtectData.argtypes = [
    POINTER(_DataBlob), wt.LPCWSTR, POINTER(_DataBlob), c_void_p, c_void_p, wt.DWORD, POINTER(_DataBlob),
]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    POINTER(_DataBlob), POINTER(wt.LPCWSTR), POINTER(_DataBlob), c_void_p, c_void_p, wt.DWORD, POINTER(_DataBlob),
]
_crypt32.CryptUnprotectData.restype = wt.BOOL


def _blob_from_bytes(data: bytes) -> _DataBlob:
    array = (c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(array, POINTER(c_ubyte)))


def _bytes_from_blob(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _encrypt(data: bytes) -> bytes:
    blob_in = _blob_from_bytes(data)
    blob_out = _DataBlob()
    if not _crypt32.CryptProtectData(byref(blob_in), "native_wallpaper_manager_session", None, None, None, 0, byref(blob_out)):
        raise OSError("CryptProtectData 加密失败")
    return _bytes_from_blob(blob_out)


def _decrypt(data: bytes) -> bytes:
    blob_in = _blob_from_bytes(data)
    blob_out = _DataBlob()
    if not _crypt32.CryptUnprotectData(byref(blob_in), None, None, None, None, 0, byref(blob_out)):
        raise OSError("CryptUnprotectData 解密失败")
    return _bytes_from_blob(blob_out)


def save_session(token: str, open_id: str, device_id: str) -> None:
    payload = json.dumps({"token": token, "open_id": open_id, "device_id": device_id}).encode("utf-8")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_bytes(_encrypt(payload))


def load_session() -> tuple[str, str, str] | None:
    try:
        data = SESSION_PATH.read_bytes()
        payload = json.loads(_decrypt(data).decode("utf-8"))
        token = str(payload.get("token") or "")
        open_id = str(payload.get("open_id") or "")
        device_id = str(payload.get("device_id") or "")
        if token and open_id and device_id:
            return token, open_id, device_id
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return None


def clear_session() -> None:
    try:
        SESSION_PATH.unlink(missing_ok=True)
    except OSError:
        pass
