import ctypes
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import wallpaper_backend as backend


def signed(value):
    return ctypes.c_long(value).value


class FakeOle:
    """A real ctypes vtable, backed by offline Python callbacks."""

    def __init__(self, *, initialized=0, created=0, applied=0):
        self.events = []
        self.created = created
        self.applied = applied
        self.CoInitializeEx = Mock(side_effect=lambda *_: self.note("init", initialized))
        self.CoUninitialize = Mock(side_effect=lambda: self.note("uninit"))
        self.CoCreateInstance = Mock(side_effect=self.create)
        self.release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
            lambda *_: self.note("release", 0))
        self.setter = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                        ctypes.c_wchar_p, ctypes.c_wchar_p)(self.set_wallpaper)
        self.table = (ctypes.c_void_p * 4)(0, 0, ctypes.cast(self.release, ctypes.c_void_p).value,
                                         ctypes.cast(self.setter, ctypes.c_void_p).value)
        self.object = ctypes.pointer(ctypes.cast(self.table, ctypes.POINTER(ctypes.c_void_p)))

    def note(self, event, result=None):
        self.events.append((event, threading.get_ident()))
        return result

    def create(self, clsid, outer, context, iid, output):
        self.note("create")
        self.context = context
        self.clsid = ctypes.string_at(clsid, 16)
        self.iid = ctypes.string_at(iid, 16)
        if self.created >= 0:
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(self.object, ctypes.c_void_p)
        return self.created

    def set_wallpaper(self, _instance, monitor, path):
        self.note("set")
        self.request = (monitor, path)
        return self.applied


class ShellWallpaperTests(unittest.TestCase):
    def invoke(self, ole):
        with patch.object(backend.ctypes, "WinDLL", return_value=ole):
            backend._set_desktop_wallpaper(Path("wallpaper.jpg"))

    def test_apartment_references_and_interface_released_on_calling_thread(self):
        for initialized in (0, 1, signed(0x80010106)):
            with self.subTest(initialized=initialized):
                ole = FakeOle(initialized=initialized)
                errors = []
                def run():
                    try:
                        self.invoke(ole)
                    except Exception as error:
                        errors.append(error)
                worker = threading.Thread(target=run)
                worker.start()
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                expected = ["init", "create", "set", "release"]
                if initialized >= 0:
                    expected.append("uninit")
                self.assertEqual([event for event, _ in ole.events], expected)
                self.assertTrue(all(tid == worker.ident for _, tid in ole.events))
                self.assertEqual(ole.request, (None, "wallpaper.jpg"))
                self.assertEqual(ole.context, 23)
                self.assertEqual(ole.clsid, UUID("C2CF3110-460E-4FC1-B9D0-8A1C0C9CC4BD").bytes_le)
                self.assertEqual(ole.iid, UUID("B92B56A9-8B55-4E14-9A89-0199BBB6F93B").bytes_le)

    def test_hresult_failures_cleanup_without_spi_fallback(self):
        for kwargs, expected in [
            ({"initialized": signed(0x80070005)}, ["init"]),
            ({"created": signed(0x80040154)}, ["init", "create", "uninit"]),
            ({"applied": signed(0x80070005)}, ["init", "create", "set", "release", "uninit"]),
        ]:
            with self.subTest(kwargs=kwargs):
                ole = FakeOle(**kwargs)
                with patch.object(backend, "_system_parameters_info") as spi:
                    with self.assertRaisesRegex(OSError, "HRESULT 0x800"):
                        self.invoke(ole)
                    spi.assert_not_called()
                self.assertEqual([event for event, _ in ole.events], expected)

    def test_default_adapter_uses_shell_once_and_preserves_pending_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "wallpaper.jpg"
            target.write_bytes(b"image")
            with patch.object(backend, "_set_desktop_wallpaper") as shell, patch.object(
                backend, "_system_parameters_info") as spi:
                result = backend.set_native_wallpaper(target, confirm_timeout=0,
                                                       current_reader=lambda: Path(temp) / "old.jpg")
                shell.assert_called_once_with(target.resolve())
                spi.assert_not_called()
                self.assertFalse(result.confirmed)
                self.assertEqual(result.attempts, 1)
            with patch.object(backend, "_set_desktop_wallpaper", side_effect=OSError("Shell rejected")):
                reader = Mock()
                with self.assertRaisesRegex(OSError, "Shell rejected"):
                    backend.set_native_wallpaper(target, current_reader=reader)
                reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
