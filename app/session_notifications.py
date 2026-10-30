"""Receive session changes for the Tk window using the Windows WTS API."""
import ctypes
from ctypes import wintypes

WM_WTSSESSION_CHANGE = 0x02B1
RESTORE_EVENTS = {1, 3, 5, 8}  # console connect, remote connect, logon, unlock
GWLP_WNDPROC = -4


class SessionNotifications:
    def __init__(self, root, on_restore):
        self.hwnd = None
        self.previous = None
        self.callback = None
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self.user32, self.wts = user32, wts
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        wts.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
        wts.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        def proc(window, message, wparam, lparam):
            if message == WM_WTSSESSION_CHANGE and wparam in RESTORE_EVENTS:
                try:
                    on_restore()
                except Exception:
                    pass  # Never unwind a Python exception through a Windows callback.
            return user32.CallWindowProcW(self.previous, window, message, wparam, lparam)

        self.callback = proc_type(proc)
        previous = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(self.callback, ctypes.c_void_p).value)
        if not previous:
            raise ctypes.WinError(ctypes.get_last_error())
        self.previous, self.hwnd = previous, hwnd
        if not wts.WTSRegisterSessionNotification(hwnd, 0):
            error = ctypes.WinError(ctypes.get_last_error())
            user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, previous)
            self.hwnd = None
            raise error

    def close(self):
        if self.hwnd:
            self.wts.WTSUnRegisterSessionNotification(self.hwnd)
            self.user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self.previous)
            self.hwnd = None
