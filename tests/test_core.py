import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
import native_wallpaper as app
from catalog import IntegrationUnavailable, YuanqiOfficialApiSource
from yuanqi_auth import (
    AuthPhase,
    BRIDGE_METHODS,
    DEFAULT_LOGIN_PROVIDER,
    LOGIN_OPTIONS,
    PHONE_LOGIN_SURFACE,
    QQ_LOGIN_SURFACE,
    QQLoginMethod,
    LoginInteraction,
    LoginProvider,
    UnavailableYuanqiLoginBackend,
    YuanqiAuthCoordinator,
    YuanqiSession,
)


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

    def test_yuanqi_bridge_contract_matches_official_h5(self):
        methods = {(method.channel, method.callback) for method in BRIDGE_METHODS}
        self.assertEqual(methods, {
            ("navigate2Login", "callbackOnLogin"),
            ("getUserInfo", "callbackUserInfo"),
            ("getDeviceInfo", "callbackDeviceInfo"),
        })

    def test_unconfigured_yuanqi_login_stays_unavailable(self):
        auth = YuanqiAuthCoordinator(UnavailableYuanqiLoginBackend())
        self.assertEqual(auth.begin_login().phase, AuthPhase.UNAVAILABLE)
        self.assertFalse(auth.snapshot.has_session)

    def test_yuanqi_session_repr_redacts_token(self):
        session = YuanqiSession("user-1", "device-1", "do-not-print", "QQ")
        self.assertNotIn("do-not-print", repr(session))

    def test_official_pc_login_surface_matches_observed_options(self):
        self.assertEqual(DEFAULT_LOGIN_PROVIDER, LoginProvider.WECHAT)
        self.assertEqual(
            [(option.provider, option.label) for option in LOGIN_OPTIONS],
            [
                (LoginProvider.WECHAT, "微信登录"),
                (LoginProvider.PHONE, "手机登录"),
                (LoginProvider.QQ, "QQ"),
            ],
        )
        self.assertEqual(LOGIN_OPTIONS[0].interaction, LoginInteraction.QR_SCAN)

    def test_official_phone_login_surface_matches_observed_controls(self):
        self.assertEqual(PHONE_LOGIN_SURFACE.phone_placeholder, "输入手机号")
        self.assertEqual(PHONE_LOGIN_SURFACE.code_placeholder, "输入验证码")
        self.assertEqual(PHONE_LOGIN_SURFACE.request_code_label, "获取验证码")
        self.assertEqual(PHONE_LOGIN_SURFACE.submit_label, "登录")
        self.assertFalse(PHONE_LOGIN_SURFACE.requires_consent_checkbox)

    def test_official_qq_login_surface_matches_observed_window(self):
        self.assertEqual(QQ_LOGIN_SURFACE.window_title, "QQ帐号安全登录")
        self.assertEqual(QQ_LOGIN_SURFACE.host_process, "kvipgui.exe")
        self.assertEqual(QQ_LOGIN_SURFACE.default_method, QQLoginMethod.QUICK_QR)
        self.assertEqual(QQ_LOGIN_SURFACE.password_login_label, "密码登录")
        self.assertEqual(QQ_LOGIN_SURFACE.relying_party, "金山毒霸")
        self.assertEqual(QQ_LOGIN_SURFACE.permission_scope, "使用你的QQ头像、昵称信息")

    def test_yuanqi_login_provider_state_transitions(self):
        class FakeBackend:
            def configured(self): return True
            def navigate_to_login(self): pass
            def request_phone_code(self, phone_number): pass
            def submit_phone_login(self, phone_number, verification_code): pass
            def get_user_info(self): return {}
            def get_device_info(self): return {}
            def verify_session(self, session): return False

        auth = YuanqiAuthCoordinator(FakeBackend())
        self.assertEqual(auth.begin_login().phase, AuthPhase.QR_PENDING)
        self.assertEqual(auth.snapshot.provider, LoginProvider.WECHAT.value)
        self.assertEqual(auth.select_login_provider(LoginProvider.PHONE).phase, AuthPhase.PHONE_PENDING)
        self.assertEqual(auth.select_login_provider(LoginProvider.QQ).phase, AuthPhase.QQ_QR_PENDING)
        self.assertEqual(auth.select_qq_method(QQLoginMethod.PASSWORD).phase, AuthPhase.QQ_PASSWORD_PENDING)
        self.assertEqual(auth.select_qq_method(QQLoginMethod.QUICK_QR).phase, AuthPhase.QQ_QR_PENDING)

    def test_phone_code_flow_does_not_store_phone_or_code_in_snapshot(self):
        class FakeBackend:
            def configured(self): return True
            def navigate_to_login(self): pass
            def request_phone_code(self, phone_number): self.code_requested = True
            def submit_phone_login(self, phone_number, verification_code): self.submitted = True
            def get_user_info(self): return {}
            def get_device_info(self): return {}
            def verify_session(self, session): return False

        backend = FakeBackend()
        auth = YuanqiAuthCoordinator(backend)
        auth.begin_login()
        auth.select_login_provider(LoginProvider.PHONE)
        snapshot = auth.request_phone_code("13800000000")
        self.assertEqual(snapshot.phase, AuthPhase.PHONE_CODE_PENDING)
        self.assertTrue(backend.code_requested)
        snapshot = auth.submit_phone_login("13800000000", "123456")
        self.assertEqual(snapshot.phase, AuthPhase.PHONE_SUBMITTING)
        self.assertTrue(backend.submitted)
        self.assertNotIn("13800000000", repr(snapshot))
        self.assertNotIn("123456", repr(snapshot))

    def test_yuanqi_login_requires_callback_then_server_verification(self):
        class FakeBackend:
            def configured(self): return True
            def navigate_to_login(self): self.opened = True
            def request_phone_code(self, phone_number): pass
            def submit_phone_login(self, phone_number, verification_code): pass
            def get_user_info(self): return {"uid": "user-1", "token": "session-secret", "provider": "QQ"}
            def get_device_info(self): return {"deviceId": "device-1"}
            def verify_session(self, session): return session.complete

        backend = FakeBackend()
        auth = YuanqiAuthCoordinator(backend)
        self.assertEqual(auth.begin_login().phase, AuthPhase.QR_PENDING)
        self.assertTrue(backend.opened)
        result = auth.callback_on_login({"ret": 0})
        self.assertEqual(result.phase, AuthPhase.AUTHENTICATED)
        self.assertTrue(result.has_session)
        self.assertEqual(result.provider, "QQ")

    def test_legacy_featured_sources_are_migrated_and_deduplicated(self):
        title = app.OFFICIAL_FEATURED[0][0]
        config = {
            "items": [
                {"id": "old", "path": "a.png", "title": title, "favorite": False, "source": "元气官网公开精选"},
                {"id": "new", "path": "b.png", "title": title, "favorite": False, "source": app.OFFICIAL_FEATURED_SOURCE},
            ],
            "interval_minutes": 0,
            "last_applied": "new",
        }
        migrated = app.normalize_config(config)
        self.assertEqual(len(migrated["items"]), 1)
        self.assertEqual(migrated["items"][0]["source"], app.OFFICIAL_FEATURED_SOURCE)
        self.assertEqual(migrated["last_applied"], "old")

    def test_windows_path_comparison_is_case_insensitive(self):
        self.assertTrue(app.same_windows_path(Path(r"C:\\Wallpapers\\A.PNG"), Path(r"c:\\wallpapers\\a.png")))

    def test_local_gallery_uses_bounded_pages(self):
        self.assertGreater(app.LOCAL_PAGE_SIZE, 0)
        self.assertLessEqual(app.LOCAL_PAGE_SIZE, 12)

    def test_cache_discovery_rejects_empty_and_deduplicates_real_images(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "first.jpg"
            duplicate = root / "second.jpg"
            empty = root / "empty.jpg"
            Image.new("RGB", (64, 40), "#ff88aa").save(original)
            duplicate.write_bytes(original.read_bytes())
            empty.write_bytes(b"")
            found = app.discover_yuanqi_cache_images([root])
            self.assertEqual(len(found), 1)
            self.assertEqual(app.file_digest(found[0]), app.file_digest(original))

    def test_cache_copy_is_independent_and_skips_existing_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.jpg"
            existing = root / "existing.jpg"
            destination = root / "library"
            Image.new("RGB", (80, 50), "#66aaff").save(source)
            Image.new("RGB", (80, 50), "#ffaa66").save(existing)
            with patch.object(app, "WALLPAPER_DIR", destination):
                imported = app.copy_yuanqi_cache_images([source], [existing])
                self.assertEqual(len(imported), 1)
                copied = Path(imported[0].path)
                self.assertTrue(copied.is_file())
                self.assertNotEqual(copied, source)
                self.assertEqual(app.file_digest(copied), app.file_digest(source))
                self.assertEqual(app.copy_yuanqi_cache_images([source], [copied]), [])

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
