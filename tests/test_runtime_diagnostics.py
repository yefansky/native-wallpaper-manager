import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1] / "app"


class DiagnosticsTests(unittest.TestCase):
    def run_probe(self, directory, code):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(APP)
        result = subprocess.run([sys.executable, "-c", code, str(directory)], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exception_values_are_private_and_hooks_still_chain_once(self):
        with tempfile.TemporaryDirectory() as temp:
            self.run_probe(temp, """
import sys, threading
from pathlib import Path
from types import SimpleNamespace
import runtime_diagnostics as d
seen=[]
sys.excepthook=lambda *args:seen.append('main')
threading.excepthook=lambda args:seen.append('thread')
d.initialize(Path(sys.argv[1]),'test')
d.initialize(Path(sys.argv[1]),'test')
try:
    raise ValueError('secret-token https://private.example/user/123')
except ValueError as error:
    sys.excepthook(type(error),error,error.__traceback__)
    threading.excepthook(SimpleNamespace(exc_type=type(error),exc_value=error,exc_traceback=error.__traceback__))
assert seen==['main','thread'],seen
""")
            text = (Path(temp) / "runtime.log").read_text(encoding="utf-8")
            self.assertIn("ValueError", text)
            self.assertIn("frames=", text)
            self.assertNotIn("secret-token", text)
            self.assertNotIn("private.example", text)
            self.assertEqual(text.count("startup "), 1)

    def test_rotation_is_bounded_and_desktop_paths_are_hashed(self):
        with tempfile.TemporaryDirectory() as temp:
            self.run_probe(temp, """
import sys
from pathlib import Path
from unittest.mock import patch
import runtime_diagnostics as d
import wallpaper_backend as b
d.initialize(Path(sys.argv[1]),'test')
for i in range(3000):d.record('bounded',payload='x'*1000)
with patch.object(b,'_current_wallpaper_from_system_api',return_value=Path('private-account-secret.jpg')),patch.object(b,'_current_wallpaper_from_registry',return_value=Path('private-account-secret.jpg')):
    d.record_desktop('snapshot',target=Path('private-account-secret.jpg'))
""")
            logs = list(Path(temp).glob("runtime.log*"))
            self.assertEqual(len(logs), 3)
            self.assertTrue(all(path.stat().st_size <= 512 * 1024 for path in logs))
            text = (Path(temp) / "runtime.log").read_text(encoding="utf-8")
            self.assertIn("snapshot spi=", text)
            self.assertNotIn("private-account-secret", text)

    def test_diagnostics_failure_and_existing_fault_handler_preserve_host(self):
        with tempfile.TemporaryDirectory() as temp:
            self.run_probe(temp, """
import sys
from pathlib import Path
from unittest.mock import patch,Mock
import runtime_diagnostics as d
with patch.object(d.faulthandler,'is_enabled',return_value=True),patch.object(d.faulthandler,'enable') as enable:
    d.initialize(Path(sys.argv[1]),'test')
    enable.assert_not_called()
root=Mock();previous=root.report_callback_exception
root._runtime_diagnostics_installed=False
d.observe_tk(root);d.observe_tk(root)
with patch.object(d.traceback,'extract_tb',side_effect=OSError('failure')):
    root.report_callback_exception(ValueError,ValueError('secret'),None)
previous.assert_called_once()
""")
            self.assertFalse((Path(temp) / "fault.log").exists())


if __name__ == "__main__":
    unittest.main()
