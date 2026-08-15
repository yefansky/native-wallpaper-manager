import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
import native_wallpaper as app
from catalog import IntegrationUnavailable, YuanqiOfficialApiSource


class CoreTests(unittest.TestCase):
    def test_supported_extensions(self):
        self.assertIn(".jpg", app.IMAGE_EXTENSIONS)
        self.assertNotIn(".mp4", app.IMAGE_EXTENSIONS)

    def test_official_showcase_links_are_static_https_images(self):
        self.assertEqual(len(app.OFFICIAL_FEATURED), 5)
        for _, url in app.OFFICIAL_FEATURED:
            self.assertTrue(url.startswith("https://"))
            self.assertEqual(Path(url).suffix, ".png")

    def test_official_static_categories_are_https_pages(self):
        self.assertGreaterEqual(len(app.OFFICIAL_STATIC_CATEGORIES), 10)
        self.assertTrue(all(url.startswith("https://bizhi.cheetahfun.com/") for _, url in app.OFFICIAL_STATIC_CATEGORIES))

    def test_unconfigured_official_api_never_returns_fake_data(self):
        source = YuanqiOfficialApiSource()
        capabilities = source.capabilities()
        self.assertFalse(capabilities.authorization)
        self.assertFalse(capabilities.original_download)
        with self.assertRaises(IntegrationUnavailable):
            source.list_items("landscape")

    def test_no_desktop_overlay_or_explorer_injection(self):
        source = (Path(__file__).parents[1] / "app" / "native_wallpaper.py").read_text(encoding="utf-8")
        forbidden = ("WorkerW", "SetParent", "Progman", "FindWindowW", "ShowWindow", "SHELLDLL_DefView")
        for symbol in forbidden:
            self.assertNotIn(symbol, source)
        self.assertIn("SystemParametersInfoW", source)


    def test_config_round_trip(self):
        original = app.CONFIG_PATH
        with tempfile.TemporaryDirectory() as temp:
            app.CONFIG_PATH = Path(temp) / "config.json"
            value = {"items": [], "interval_minutes": 5, "last_applied": "x"}
            app.save_config(value)
            self.assertEqual(app.load_config()["interval_minutes"], 5)
        app.CONFIG_PATH = original


if __name__ == "__main__":
    unittest.main()
