import ctypes
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
import native_wallpaper as app
from session_notifications import SessionNotifications, WM_WTSSESSION_CHANGE


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "saved.jpg"
        self.path.write_bytes(b"image")
        self.wallpaper = app.Wallpaper("saved", str(self.path), "saved")
        self.subject = object.__new__(app.App)
        self.subject.items = [self.wallpaper]
        self.subject.config = {"last_applied": "saved"}
        self.subject._wallpaper_restore_generation = 1
        self.subject._apply_operation_in_progress = False
        self.subject._start_wallpaper_apply = MagicMock()
        self.subject.status = MagicMock()
        self.subject.root = MagicMock()

    def check(self, persisted, current, generation=1):
        with patch.object(app, "_current_wallpaper_from_registry", return_value=persisted), patch.object(
            app, "_current_wallpaper_from_system_api", return_value=current
        ):
            self.subject._restore_saved_wallpaper(generation)

    def test_system_rollback_restored_only_when_saved_setting_matches_registry(self):
        self.check(self.path, self.path.parent / "old.jpg")
        self.subject._start_wallpaper_apply.assert_called_once_with(self.wallpaper, origin="restore", restore=True)

    def test_user_change_missing_data_and_stale_events_do_not_restore(self):
        for persisted, current, generation in [
            (self.path.parent / "user.jpg", self.path.parent / "user.jpg", 1),
            (self.path, self.path, 1), (None, self.path, 1), (self.path, None, 1),
            (self.path, self.path.parent / "old.jpg", 0),
        ]:
            self.check(persisted, current, generation)
        self.path.unlink()
        self.check(self.path, self.path.parent / "old.jpg")
        self.subject._start_wallpaper_apply.assert_not_called()

    def test_event_coalescing_and_busy_retry(self):
        self.subject._schedule_wallpaper_restore()
        self.assertEqual([c.args[0] for c in self.subject.root.after.call_args_list], [2000, 8000, 20000])
        callbacks = [c.args[1] for c in self.subject.root.after.call_args_list]
        self.subject._apply_operation_in_progress = True
        with patch.object(app, "_current_wallpaper_from_registry", return_value=self.path), patch.object(
            app, "_current_wallpaper_from_system_api", return_value=self.path.parent / "old.jpg"
        ):
            callbacks[0]()
            self.subject._apply_operation_in_progress = False
            callbacks[1]()
        self.subject._start_wallpaper_apply.assert_called_once()

    def test_windows_message_registration_and_cleanup(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        self.addCleanup(root.destroy)
        seen = []
        listener = SessionNotifications(root, lambda: seen.append(True))
        hwnd = listener.hwnd
        send = ctypes.windll.user32.SendMessageW
        send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        send.restype = ctypes.c_ssize_t
        send(hwnd, WM_WTSSESSION_CHANGE, 3, 0)
        send(hwnd, WM_WTSSESSION_CHANGE, 4, 0)
        self.assertEqual(seen, [True])
        listener.close()
        send(hwnd, WM_WTSSESSION_CHANGE, 3, 0)
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
