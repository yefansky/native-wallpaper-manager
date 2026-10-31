import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
import native_wallpaper as app
import session_store as session_store
import wallpaper_backend as backend
from product import SingleInstance, autostart_command, autostart_enabled, set_autostart
from catalog import (
    CatalogError,
    CatalogCategory,
    CatalogItem,
    CatalogPage,
    IntegrationUnavailable,
    YuanqiDynamicResourceSource,
    YuanqiOfficialApiSource,
    YuanqiResourceSource,
    YuanqiThemeResourceSource,
    validate_resource_preview_video_url,
)
import dynamic_converter
from dynamic_converter import (
    ConversionUnavailable,
    ConvertedFrame,
    candidate_timestamps,
    convert_preview_video_to_jpeg,
    decrypt_video_bytes,
    find_ffmpeg_tools,
    frame_quality,
    load_cached_frame,
    store_cached_frame,
)
from yuanqi_auth import (
    AuthPhase,
    BRIDGE_METHODS,
    DEFAULT_LOGIN_PROVIDER,
    KVIPGUI_LOGIN_BRIDGE_BOUNDARY,
    LOGIN_OPTIONS,
    PHONE_LOGIN_SURFACE,
    QQ_LOGIN_SURFACE,
    QQ_AUTH_RETURN_SURFACE,
    QQLoginMethod,
    WECHAT_AUTH_RETURN_SURFACE,
    WECHAT_LOGIN_SURFACE,
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

    def test_official_wechat_login_uses_unified_native_host(self):
        self.assertEqual(WECHAT_LOGIN_SURFACE.host_process, "kvipgui.exe")
        self.assertEqual(
            WECHAT_LOGIN_SURFACE.observed_arguments,
            ("--open_web=login", "--pid=10", "--vipfrom=1035"),
        )
        self.assertEqual(WECHAT_LOGIN_SURFACE.default_provider, LoginProvider.WECHAT)
        self.assertTrue(WECHAT_LOGIN_SURFACE.requires_phone_confirmation)
        self.assertTrue(WECHAT_AUTH_RETURN_SURFACE.external_host_closed)
        self.assertTrue(WECHAT_AUTH_RETURN_SURFACE.main_host_survives)
        self.assertTrue(WECHAT_AUTH_RETURN_SURFACE.refresh_requires_official_callback)
        self.assertFalse(WECHAT_AUTH_RETURN_SURFACE.child_exit_is_authentication_proof)
        self.assertFalse(WECHAT_AUTH_RETURN_SURFACE.main_host_waits_for_child_process)
        self.assertEqual(
            WECHAT_AUTH_RETURN_SURFACE.official_listener_interface,
            "vip_sdk::IUserInfoChangeListener",
        )
        self.assertEqual(WECHAT_AUTH_RETURN_SURFACE.observed_main_listener, "KGalleryDlg")

    def test_kvipgui_internal_bridge_does_not_authorize_credential_replay(self):
        boundary = KVIPGUI_LOGIN_BRIDGE_BOUNDARY
        self.assertEqual(boundary.refresh_method, "refreshUserInfo")
        self.assertEqual(boundary.state_query_method, "isUserLogin")
        self.assertEqual(boundary.credential_ingress_method, "userLogin")
        self.assertEqual(boundary.result_field, "result")
        self.assertEqual(boundary.credential_field_names, ("token", "open_id"))
        self.assertEqual(boundary.self_close_window_message, "WM_CLOSE")
        self.assertFalse(boundary.direct_credential_replay_supported)

    def test_official_qq_login_surface_matches_observed_window(self):
        self.assertEqual(QQ_LOGIN_SURFACE.window_title, "QQ帐号安全登录")
        self.assertEqual(QQ_LOGIN_SURFACE.host_process, "kvipgui.exe")
        self.assertEqual(QQ_LOGIN_SURFACE.default_method, QQLoginMethod.QUICK_QR)
        self.assertEqual(QQ_LOGIN_SURFACE.password_login_label, "密码登录")
        self.assertEqual(QQ_LOGIN_SURFACE.relying_party, "金山毒霸")
        self.assertEqual(QQ_LOGIN_SURFACE.permission_scope, "使用你的QQ头像、昵称信息")

    def test_qq_auth_return_surface_matches_observed_host_refresh(self):
        self.assertTrue(QQ_AUTH_RETURN_SURFACE.external_window_closed)
        self.assertTrue(QQ_AUTH_RETURN_SURFACE.quick_login_dialog_closed)
        self.assertTrue(QQ_AUTH_RETURN_SURFACE.account_avatar_replaces_login_button)
        self.assertEqual(QQ_AUTH_RETURN_SURFACE.authenticated_tab_label, "关注")

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
        self.assertEqual(auth.begin_login().phase, AuthPhase.WECHAT_QR_PENDING)
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
        events = []

        class FakeBackend:
            def configured(self): return True
            def navigate_to_login(self): self.opened = True; events.append("navigate")
            def request_phone_code(self, phone_number): pass
            def submit_phone_login(self, phone_number, verification_code): pass
            def get_user_info(self): events.append("user"); return {"uid": "user-1", "token": "session-secret", "provider": "QQ"}
            def get_device_info(self): events.append("device"); return {"deviceId": "device-1"}
            def verify_session(self, session): events.append("verify"); return session.complete

        backend = FakeBackend()
        auth = YuanqiAuthCoordinator(backend)
        self.assertEqual(auth.begin_login().phase, AuthPhase.WECHAT_QR_PENDING)
        self.assertTrue(backend.opened)
        result = auth.callback_on_login({"ret": 0})
        self.assertEqual(result.phase, AuthPhase.AUTHENTICATED)
        self.assertTrue(result.has_session)
        self.assertEqual(result.provider, "QQ")
        self.assertEqual(events, ["navigate", "user", "device", "verify"])

    def test_visible_host_refresh_does_not_bypass_login_callback(self):
        class FakeBackend:
            def configured(self): return True
            def navigate_to_login(self): pass
            def request_phone_code(self, phone_number): pass
            def submit_phone_login(self, phone_number, verification_code): pass
            def get_user_info(self): raise AssertionError("失败回调后不应读取用户信息")
            def get_device_info(self): raise AssertionError("失败回调后不应读取设备信息")
            def verify_session(self, session): raise AssertionError("失败回调后不应校验会话")

        auth = YuanqiAuthCoordinator(FakeBackend())
        auth.begin_login()
        auth.select_login_provider(LoginProvider.QQ)
        observed = auth.observe_official_user_info_change()
        self.assertEqual(observed.phase, AuthPhase.OFFICIAL_REFRESH_OBSERVED)
        self.assertFalse(observed.has_session)
        result = auth.callback_on_login(False)
        self.assertEqual(result.phase, AuthPhase.SIGNED_OUT)
        self.assertFalse(result.has_session)

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

    def test_local_global_search_matches_title_source_and_filename(self):
        item = app.Wallpaper("1", r"C:\\Wallpapers\\forest-night.jpg", "雨夜森林", source="本机元气缓存")
        self.assertTrue(app.wallpaper_matches_query(item, "雨夜"))
        self.assertTrue(app.wallpaper_matches_query(item, "元气 缓存"))
        self.assertTrue(app.wallpaper_matches_query(item, "forest"))
        self.assertFalse(app.wallpaper_matches_query(item, "海边"))

    def test_resource_navigation_matches_yuanqi_order(self):
        labels = [category.title for category in YuanqiResourceSource().list_categories()]
        # 元气静态页只把这两个入口写在前端；其余导航由 cate_nav_v2 返回。
        self.assertEqual(labels, ["关注", "精选"])

    def test_dynamic_navigation_has_official_special_entries(self):
        labels = [category.title for category in YuanqiDynamicResourceSource().list_categories()]
        self.assertEqual(labels, ["关注", "精选", "最新"])

    def test_dynamic_featured_marks_first_five_as_today_hot(self):
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}
        )
        raw_items = [
            {
                "wid": index,
                "wname": f"热榜{index}",
                "preview_jpg_raw": f"https://img-baofun.zhhainiao.com/{index}.jpg",
                "preview_video": f"https://img-baofun.zhhainiao.com/{index}_preview.mp4",
            }
            for index in range(1, 7)
        ]
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0}, "data": {"items": raw_items, "total": 24}
            }
            page = source.list_items("__featured__", 1)
        self.assertEqual([item.id for item in page.featured_items], ["1", "2", "3", "4", "5"])
        self.assertEqual(len(page.items), 6)

    def test_dynamic_candidate_sampling_never_uses_first_frame(self):
        points = candidate_timestamps(10.0)
        self.assertEqual(len(points), 5)
        self.assertGreater(points[0], 1.0)
        self.assertLess(points[-1], 9.0)

    def test_xor_video_decryption_keeps_key_alignment_across_chunks(self):
        plain = bytes(range(255)) * 9
        encrypted = decrypt_video_bytes(plain)
        split = 317
        decoded = decrypt_video_bytes(encrypted[:split], 0) + decrypt_video_bytes(encrypted[split:], split)
        self.assertEqual(decoded, plain)

    def test_dynamic_frame_quality_prefers_sharp_picture(self):
        from PIL import ImageFilter

        sharp = Image.new("RGB", (1280, 720), "white")
        for x in range(0, 1280, 16):
            for y in range(0, 720, 16):
                if (x // 16 + y // 16) % 2:
                    sharp.paste("black", (x, y, x + 16, y + 16))
        blurred = sharp.filter(ImageFilter.GaussianBlur(8))
        self.assertGreater(frame_quality(sharp), frame_quality(blurred))

    def test_conversion_tools_prefer_local_converter_pack(self):
        # 首次转换下载的组件放在本机数据目录，必须先于程序目录和系统 PATH。
        with tempfile.TemporaryDirectory() as temp, patch.object(
            dynamic_converter, "CONVERTER_TOOL_DIR", Path(temp) / "converter" / "bin"
        ), patch.object(dynamic_converter, "_bundled_tool_bases", return_value=()):
            tool_dir = Path(dynamic_converter.CONVERTER_TOOL_DIR)
            tool_dir.mkdir(parents=True)
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                (tool_dir / name).write_bytes(b"MZ-pack")
            with patch.object(
                dynamic_converter.shutil, "which", side_effect=AssertionError("local pack must win over PATH")
            ):
                self.assertEqual(
                    find_ffmpeg_tools(),
                    (str(tool_dir / "ffmpeg.exe"), str(tool_dir / "ffprobe.exe")),
                )

    def test_conversion_tools_prefer_bundled_package_directory(self):
        # 源码运行或手工放置的 tools\ffmpeg\bin 优先于系统 PATH，避免依赖用户机器。
        with tempfile.TemporaryDirectory() as temp:
            installed = Path(temp)
            bundled = installed / "tools" / "ffmpeg" / "bin"
            bundled.mkdir(parents=True)
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                (bundled / name).write_bytes(b"MZ-bundled")
            with patch.object(dynamic_converter, "CONVERTER_TOOL_DIR", installed / "pack" / "bin"), patch.object(
                dynamic_converter, "_bundled_tool_bases", return_value=(installed,)
            ), patch.object(
                dynamic_converter.shutil, "which", side_effect=AssertionError("bundled tools must win over PATH")
            ):
                self.assertEqual(
                    find_ffmpeg_tools(),
                    (str(bundled / "ffmpeg.exe"), str(bundled / "ffprobe.exe")),
                )

    def test_conversion_tools_fall_back_to_path_when_package_is_unbundled(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            dynamic_converter, "CONVERTER_TOOL_DIR", Path(temp) / "pack" / "bin"
        ), patch.object(
            dynamic_converter, "_bundled_tool_bases", return_value=(Path(temp),)
        ), patch.object(dynamic_converter.shutil, "which", side_effect=lambda name: f"C:/tools/{name}.exe"):
            self.assertEqual(find_ffmpeg_tools(), ("C:/tools/ffmpeg.exe", "C:/tools/ffprobe.exe"))

    def test_missing_conversion_tools_message_explains_automatic_download(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            dynamic_converter, "CONVERTER_TOOL_DIR", Path(temp) / "pack" / "bin"
        ), patch.object(
            dynamic_converter, "_bundled_tool_bases", return_value=(Path(temp),)
        ), patch.object(dynamic_converter.shutil, "which", return_value=None), patch.object(
            dynamic_converter, "_bundled_winget_tool", return_value=None
        ):
            with self.assertRaises(ConversionUnavailable) as raised:
                find_ffmpeg_tools()
        message = str(raised.exception)
        self.assertIn("自动从 GitHub 下载", message)
        self.assertIn(r"tools\ffmpeg\bin", message)

    def test_converter_pack_download_verifies_checksum_and_installs(self):
        import hashlib
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("ffmpeg.exe", b"MZ-ffmpeg")
            archive.writestr("ffprobe.exe", b"MZ-ffprobe")
            archive.writestr("LICENSE.txt", b"LGPL")
        payload = buffer.getvalue()
        digest = hashlib.sha256(payload).hexdigest()

        class FakeResponse:
            def __init__(self, content=b"", text=""):
                self.content = content
                self.text = text

            def raise_for_status(self):
                return None

            def iter_content(self, size):
                yield self.content

        class FakeHttp:
            def get(self, url, timeout, stream=False):
                if url.endswith(".sha256"):
                    return FakeResponse(text=f"{digest}  pack.zip")
                return FakeResponse(content=payload)

        with tempfile.TemporaryDirectory() as temp, patch.object(
            dynamic_converter, "CONVERTER_TOOL_DIR", Path(temp) / "converter" / "bin"
        ), patch.object(dynamic_converter, "_bundled_tool_bases", return_value=()), patch.object(
            dynamic_converter.shutil, "which", return_value=None
        ), patch.object(dynamic_converter, "_bundled_winget_tool", return_value=None):
            ffmpeg, ffprobe = dynamic_converter.install_converter_pack(http=FakeHttp())
            tool_dir = Path(dynamic_converter.CONVERTER_TOOL_DIR)
            self.assertEqual(Path(ffmpeg), tool_dir / "ffmpeg.exe")
            self.assertEqual(Path(ffprobe), tool_dir / "ffprobe.exe")
            self.assertTrue((tool_dir / "LICENSE.txt").is_file())
            self.assertEqual((tool_dir / "ffmpeg.exe").read_bytes(), b"MZ-ffmpeg")
            # 已安装后再次调用不再下载。
            self.assertEqual(dynamic_converter.install_converter_pack(http=FakeHttp()), (ffmpeg, ffprobe))

    def test_converter_pack_rejects_bad_checksum(self):
        import hashlib

        class FakeResponse:
            def __init__(self, content=b"", text=""):
                self.content = content
                self.text = text

            def raise_for_status(self):
                return None

            def iter_content(self, size):
                yield self.content

        class FakeHttp:
            def get(self, url, timeout, stream=False):
                if url.endswith(".sha256"):
                    return FakeResponse(text=hashlib.sha256(b"different").hexdigest())
                return FakeResponse(content=b"not-a-zip")

        with tempfile.TemporaryDirectory() as temp, patch.object(
            dynamic_converter, "CONVERTER_TOOL_DIR", Path(temp) / "converter" / "bin"
        ), patch.object(dynamic_converter, "_bundled_tool_bases", return_value=()), patch.object(
            dynamic_converter.shutil, "which", return_value=None
        ), patch.object(dynamic_converter, "_bundled_winget_tool", return_value=None):
            with self.assertRaises(ConversionUnavailable) as raised:
                dynamic_converter.install_converter_pack(http=FakeHttp())
            self.assertIn("校验失败", str(raised.exception))
            self.assertFalse(Path(dynamic_converter.CONVERTER_TOOL_DIR).exists())

    def test_dynamic_conversion_cache_is_validated_and_bypasses_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            output = io.BytesIO()
            Image.new("RGB", (1280, 720), "#336699").save(output, format="JPEG", quality=95)
            url = "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/preview/cache_preview.mp4"
            original = ConvertedFrame(output.getvalue(), 1280, 720, 2.5, 88.0, 5)
            store_cached_frame(url, original, Path(temp))

            finder = unittest.mock.Mock(side_effect=AssertionError("cache hit must not invoke ffmpeg"))
            cached = convert_preview_video_to_jpeg(url, cache_dir=Path(temp), tool_finder=finder)

            finder.assert_not_called()
            self.assertTrue(cached.from_cache)
            self.assertEqual((cached.width, cached.height), (1280, 720))
            self.assertEqual(cached.jpeg, original.jpeg)

    def test_dynamic_conversion_cache_discards_corrupt_jpeg(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_root = Path(temp)
            output = io.BytesIO()
            Image.new("RGB", (1280, 720), "#112233").save(output, format="JPEG")
            url = "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/preview/corrupt_preview.mp4"
            store_cached_frame(url, ConvertedFrame(output.getvalue(), 1280, 720, 1.0, 1.0, 5), cache_root)
            next(cache_root.glob("*.jpg")).write_bytes(b"not-a-jpeg")

            self.assertIsNone(load_cached_frame(url, cache_root))
            self.assertEqual(list(cache_root.iterdir()), [])

    def test_dynamic_preview_rejects_full_video_path(self):
        with self.assertRaises(CatalogError):
            validate_resource_preview_video_url(
                "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/live/protected.mp4"
            )

    def test_cwp3_source_raises_instead_of_xor_garbage(self):
        # CWP 容器源不能被当作 XOR-MP4 解出乱码；应明确拒绝，避免假时钟。
        from dynamic_converter import ConversionUnavailable, convert_preview_video_to_jpeg

        class FakeHttp:
            def get(self, url, timeout, stream=False, **kwargs):
                class Resp:
                    headers = {"Content-Type": "application/octet-stream"}
                    def raise_for_status(self):
                        return None
                    def iter_content(self, _n):
                        yield b"CWP\x03" + b"\x00" * 64
                return Resp()

        with self.assertRaises(ConversionUnavailable) as ctx:
            convert_preview_video_to_jpeg(
                "https://img-baofun.zhhainiao.com/scene/x.cwp",
                http=FakeHttp(),
                tool_finder=lambda: ("ffmpeg.exe", "ffprobe.exe"),  # 不会真的被调用
                decrypt=True,
            )
        self.assertIn("CWP", str(ctx.exception))

    def test_only_dynamic_scene_items_receive_baked_component_warning(self):
        scene = CatalogItem("1", "场景", "https://example.test/scene.jpg", resource_kind="dynamic", format="scene")
        video = CatalogItem("2", "视频", "https://example.test/video.jpg", resource_kind="dynamic", format="MP4")
        static = CatalogItem("3", "静态", "https://example.test/static.jpg", resource_kind="static", format="scene")
        self.assertTrue(app.scene_components_may_be_baked(scene))
        self.assertFalse(app.scene_components_may_be_baked(video))
        self.assertFalse(app.scene_components_may_be_baked(static))

    def test_scene_conversion_confirmation_defaults_to_safe_cancel(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            scene = CatalogItem(
                "1", "场景", "https://example.test/scene.jpg", resource_kind="dynamic", format="scene"
            )
            with patch.object(app.messagebox, "askokcancel", return_value=False) as confirm:
                self.assertFalse(window._confirm_scene_conversion(scene))
            self.assertEqual(confirm.call_args.kwargs["default"], "cancel")
            self.assertIn("已取消转换", window.status.get())
        finally:
            root.destroy()

    def test_unverified_synthetic_category_is_not_faked(self):
        source = YuanqiResourceSource(lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"})
        with self.assertRaises(CatalogError):
            source.list_items("__ai__")

    def test_app_builds_and_global_search_leaves_current_category(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            root.update_idletasks()
            self.assertEqual(window._current_page, "wallpaper")
            self.assertEqual(
                [button.cget("text") for button in window.online_cate_buttons.values()],
                ["关注", "精选"],
            )
            window.online_page = 4
            window.global_search_var.set("雨夜")
            with patch.object(window, "load_online_items") as load_online:
                window.run_global_search()
            self.assertEqual(window._current_page, "official")
            self.assertEqual(window.online_cate_id, "__featured__")
            self.assertEqual(window.online_page, 1)
            self.assertEqual(window.online_query, "雨夜")
            self.assertEqual(window.local_query, "雨夜")
            load_online.assert_called_once_with("__featured__", 1, "雨夜")
        finally:
            root.destroy()

    def test_app_switches_dynamic_navigation_without_changing_native_backend(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            self.assertEqual(
                [button.cget("text") for button in window.online_mode_buttons.values()],
                ["静态壁纸", "动态转静态"],
            )
            with patch.object(window, "load_online_items") as load_online:
                window._switch_resource_mode("dynamic")
            self.assertEqual(window.online_resource_mode, "dynamic")
            self.assertEqual(
                [button.cget("text") for button in window.online_cate_buttons.values()],
                ["关注", "精选", "最新"],
            )
            load_online.assert_called_once_with("__featured__", 1)
        finally:
            root.destroy()

    def test_dynamic_featured_renders_five_manual_controls_and_auto_rotation(self):
        root = app.tk.Tk()
        root.withdraw()
        window = None
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            window.online_resource_mode = "dynamic"
            window.online_cate_id = "__featured__"
            window.online_page = 1
            items = tuple(
                CatalogItem(
                    str(index), f"今日热榜{index}", f"https://img-baofun.zhhainiao.com/{index}.jpg",
                    resource_kind="dynamic",
                )
                for index in range(1, 7)
            )
            result = CatalogPage(items, 1, True, 24, items[:5])
            with patch.object(window, "_queue_featured_thumbnail"), patch.object(window, "_queue_online_thumbnail"):
                window._set_online_items(result)
                root.update_idletasks()
            self.assertEqual(window._featured_carousel_kind, "dynamic")
            self.assertEqual(len(window._featured_indicator_buttons), 5)
            self.assertEqual([button.cget("text") for button in window._featured_indicator_buttons], list("12345"))
            self.assertEqual(window._featured_hot_label.cget("text"), "今日热榜")
            self.assertEqual(window._featured_dynamic_footer.winfo_manager(), "place")
            self.assertTrue(all(
                button.master.master is window._featured_dynamic_footer
                for button in window._featured_indicator_buttons
            ))
            self.assertEqual(
                (window._featured_main_label._photo.width(), window._featured_main_label._photo.height()),
                (960, 400),
            )

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            action_labels = {
                widget.cget("text") for widget in descendants(window._featured_dynamic_footer)
                if isinstance(widget, app.tk.Button)
            }
            self.assertTrue({"转换到本地", "转换并设为壁纸"}.issubset(action_labels))
            self.assertEqual([item.id for item in window.online_grid_items], ["6"])
            self.assertIsNotNone(window._featured_carousel_after_id)
            root.after_cancel(window._featured_carousel_after_id)
            window._featured_carousel_after_id = None
            window._advance_featured_carousel(window._preview_generation)
            self.assertEqual(window._featured_carousel_index, 1)
            self.assertEqual(window.online_selected_id, "dynamic:1")
            self.assertIsNotNone(window._featured_carousel_after_id)
            window._set_featured_carousel_index(3, select=True)
            self.assertEqual(window._featured_carousel_index, 3)
            self.assertEqual(window.online_selected_id, "dynamic:4")
            self.assertEqual(window._featured_title_label.cget("text"), "今日热榜4")
            window._clear_online_grid()
            self.assertIsNone(window._featured_carousel_after_id)
        finally:
            if window is not None:
                window._cancel_featured_carousel()
                window._thumbnail_executor.shutdown(wait=True, cancel_futures=True)
            root.update()
            for callback_id in root.tk.splitlist(root.tk.call("after", "info")):
                root.after_cancel(callback_id)
            root.destroy()

    def test_theme_featured_renders_hot_theme_strip_with_left_right_switching(self):
        root = app.tk.Tk()
        root.withdraw()
        window = None
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            window.online_resource_mode = "theme"
            window.online_cate_id = "__featured__"
            window.online_page = 1
            items = tuple(
                CatalogItem(
                    str(index), f"热门主题{index}", f"https://img-baofun.zhhainiao.com/theme-{index}.jpg",
                    resource_kind="theme",
                )
                for index in range(1, 5)
            )
            result = CatalogPage(items, 1, True, 0, items[:3])
            with patch.object(window, "_queue_featured_thumbnail"), patch.object(window, "_queue_online_thumbnail"):
                window._set_online_items(result)
                root.update_idletasks()
            self.assertEqual(window._featured_carousel_kind, "theme")
            self.assertEqual(len(window._featured_theme_labels), 3)
            self.assertEqual([item.id for item in window.online_grid_items], ["4"])
            self.assertIsNone(window._featured_carousel_after_id)
            window._change_featured_carousel(1)
            self.assertEqual(window._featured_carousel_index, 1)
            self.assertEqual(window.online_selected_id, "theme:2")
        finally:
            if window is not None:
                window._cancel_featured_carousel()
                window._thumbnail_executor.shutdown(wait=True, cancel_futures=True)
            root.update()
            for callback_id in root.tk.splitlist(root.tk.call("after", "info")):
                root.after_cancel(callback_id)
            root.destroy()

    def test_computer_theme_nav_uses_theme_catalog_as_its_own_page(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            with patch.object(window, "load_online_items") as load_online:
                window.show_page("theme")
            self.assertEqual(window._current_page, "theme")
            self.assertEqual(window.online_resource_mode, "theme")
            self.assertIs(window._active_online_source(), window.yuanqi_theme_source)
            self.assertEqual([button.cget("text") for button in window.online_cate_buttons.values()], ["精选"])
            self.assertEqual(window.online_mode_row.winfo_manager(), "")
            load_online.assert_called_once_with("__featured__", 1)
        finally:
            root.destroy()

    def test_theme_conversion_confirmation_is_explicit_and_defaults_to_cancel(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            theme = CatalogItem(
                "theme-1", "普通主题", "https://example.test/theme.jpg",
                resource_kind="theme", format="scene",
                source_video_url="https://img-baofun.zhhainiao.com/theme.mp4",
            )
            with patch.object(app.messagebox, "askokcancel", return_value=False) as confirm:
                self.assertFalse(window._confirm_scene_conversion(theme))
            self.assertEqual(confirm.call_args.kwargs["default"], "cancel")
            self.assertIn("Windows 原生 .theme", confirm.call_args.args[1])
            self.assertIn("已取消转换", window.status.get())
        finally:
            root.destroy()

    def test_theme_with_live_components_and_private_package_is_blocked(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            theme = CatalogItem(
                "theme-time", "随真实时间变化", "https://example.test/theme.jpg",
                resource_kind="theme", format="scene",
                source_video_url="https://img-baofun.zhhainiao.com/theme.zip",
                preview_video_url="https://img-baofun.zhhainiao.com/theme_preview.mp4",
            )
            with patch.object(app.messagebox, "showwarning") as warning, patch.object(
                app.messagebox, "askokcancel"
            ) as confirm:
                self.assertFalse(window._confirm_scene_conversion(theme))
            warning.assert_called_once()
            confirm.assert_not_called()
            self.assertIn("固定假时钟", warning.call_args.args[1])
            self.assertIn("已阻止转换", window.status.get())
        finally:
            root.destroy()

    def test_scene_with_private_container_source_is_blocked_before_download(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            scene = CatalogItem(
                "scene-cwp", "场景", "https://example.test/scene.jpg",
                resource_kind="dynamic", format="scene",
                source_video_url="https://img-baofun.zhhainiao.com/scene.cwp",
                preview_video_url="https://img-baofun.zhhainiao.com/scene_preview.mp4",
            )
            with patch.object(app.messagebox, "showwarning") as warning, patch.object(
                app.messagebox, "askokcancel"
            ) as confirm:
                self.assertFalse(window._confirm_scene_conversion(scene))
            warning.assert_called_once()
            confirm.assert_not_called()
            self.assertIn("已阻止转换", window.status.get())
        finally:
            root.destroy()

    def test_scene_with_direct_video_source_confirms_decode_attempt(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            scene = CatalogItem(
                "scene-mp4", "场景", "https://example.test/scene.jpg",
                resource_kind="dynamic", format="scene",
                source_video_url="https://img-baofun.zhhainiao.com/scene.mp4",
                preview_video_url="https://img-baofun.zhhainiao.com/scene_preview.mp4",
            )
            with patch.object(app.messagebox, "askokcancel", return_value=True) as confirm:
                self.assertTrue(window._confirm_scene_conversion(scene))
            self.assertEqual(confirm.call_args.kwargs["default"], "ok")
            self.assertIn("不会回退带组件预览", confirm.call_args.args[1])
            self.assertNotIn("HUD 不会出现", confirm.call_args.args[1])
        finally:
            root.destroy()

    def test_theme_detail_keeps_both_actions_inside_dialog(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            theme = CatalogItem(
                "theme-detail", "详情布局", "https://example.test/theme.jpg",
                resource_kind="theme", format="scene",
            )
            with patch.object(window, "_load_detail_image"):
                dialog = window.show_online_detail(theme)
            self.assertIsNotNone(dialog)
            dialog.update_idletasks()

            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)

            actions = [
                widget for widget in descendants(dialog)
                if isinstance(widget, app.tk.Button) and "原生主题" in widget.cget("text")
            ]
            self.assertEqual(len(actions), 2)
            dialog_bottom = dialog.winfo_rooty() + dialog.winfo_height()
            self.assertTrue(all(button.winfo_rooty() + button.winfo_height() <= dialog_bottom for button in actions))
            dialog.destroy()
        finally:
            root.destroy()

    def test_single_wallpaper_theme_contains_only_native_wallpaper_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wallpaper = root / "wallpaper.jpg"
            Image.new("RGB", (1280, 720), "#224466").save(wallpaper)
            with patch.object(app, "THEME_DIR", root / "themes"):
                path = app.App._write_single_wallpaper_theme(None, wallpaper, "测试[主题]", "theme-1")
            text = path.read_text(encoding="utf-16")
            self.assertIn("[Theme]", text)
            self.assertIn("[Control Panel\\Desktop]", text)
            self.assertIn(str(wallpaper.resolve()), text)
            self.assertNotIn("CWP", text)
            self.assertNotIn("TaskbarTexture", text)

    def test_online_cards_use_three_workers_and_replace_placeholders_progressively(self):
        root = app.tk.Tk()
        root.withdraw()
        release = threading.Event()
        started = threading.Event()
        callbacks = []
        window = None
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            self.assertEqual(window._thumbnail_executor._max_workers, 3)
            item = CatalogItem("progressive", "渐进预览", "https://example.test/preview.jpg")

            def delayed_preview(_item, size):
                started.set()
                release.wait(2)
                return Image.new("RGB", size, "#ff3366")

            with patch.object(window, "_fetch_online_thumbnail_image", side_effect=delayed_preview), patch.object(
                window, "_safe_after", side_effect=callbacks.append
            ):
                before = time.perf_counter()
                window._online_card(item)
                elapsed = time.perf_counter() - before
                self.assertLess(elapsed, 0.2)
                self.assertTrue(started.wait(1))
                label = window._online_image_widgets[window._catalog_item_key(item)]
                placeholder_name = str(label.cget("image"))
                release.set()
                deadline = time.monotonic() + 2
                while not callbacks and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(callbacks)
                callbacks.pop(0)()
                self.assertNotEqual(str(label.cget("image")), placeholder_name)
        finally:
            release.set()
            if window is not None:
                window._thumbnail_executor.shutdown(wait=True, cancel_futures=True)
            root.destroy()

    def test_dynamic_cards_use_mid_preview_while_carousel_uses_large_preview(self):
        output = io.BytesIO()
        Image.new("RGB", (960, 540), "#336699").save(output, format="JPEG")
        response = unittest.mock.Mock(content=output.getvalue())
        response.raise_for_status.return_value = None
        http = unittest.mock.Mock()
        http.get.return_value = response

        class PreviewHarness:
            _thumb_cache = {}
            _thumb_cache_lock = threading.Lock()
            _thumbnail_http_local = threading.local()

        harness = PreviewHarness()
        harness._thumbnail_http_local.session = http
        item = CatalogItem(
            "dynamic-preview", "分层预览",
            "https://img-baofun.zhhainiao.com/mid.jpg",
            large_preview_url="https://img-baofun.zhhainiao.com/raw.jpg",
            resource_kind="dynamic",
            source_video_url="https://img-baofun.zhhainiao.com/protected.mp4",
        )

        card = app.App._fetch_online_thumbnail_image(harness, item, (380, 214))
        hero = app.App._fetch_online_thumbnail_image(harness, item, (960, 400))

        self.assertEqual(card.size, (380, 214))
        self.assertEqual(hero.size, (960, 400))
        self.assertEqual(
            [call.args[0] for call in http.get.call_args_list],
            [item.preview_url, item.large_preview_url],
        )
        self.assertNotIn(item.source_video_url, [call.args[0] for call in http.get.call_args_list])

    def test_global_online_search_merges_static_and_dynamic_results(self):
        root = app.tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.App, "_try_restore_session"), patch.object(app.App, "_init_tray"):
                window = app.App(root)
            static_item = CatalogItem("s1", "静态山景", "https://img-baofun.zhhainiao.com/s.jpg")
            dynamic_item = CatalogItem(
                "d1",
                "动态山景",
                "https://img-baofun.zhhainiao.com/d.jpg",
                resource_kind="dynamic",
                preview_video_url="https://img-baofun.zhhainiao.com/d_preview.mp4",
            )
            window.yuanqi_source = unittest.mock.Mock()
            window.yuanqi_dynamic_source = unittest.mock.Mock()
            window.yuanqi_source.refresh_categories.return_value = ()
            window.yuanqi_source.search.return_value = CatalogPage((static_item,), 1, False, 10)
            window.yuanqi_dynamic_source.search.return_value = CatalogPage((dynamic_item,), 1, True, 20)
            captured = []
            with patch.object(window, "_safe_after", side_effect=lambda callback: callback()), patch.object(
                window, "_set_online_items", side_effect=captured.append
            ):
                window._fetch_online("__featured__", 1, "山景")
            self.assertEqual([item.resource_kind for item in captured[0].items], ["static", "dynamic"])
            self.assertEqual(captured[0].total, 30)
            self.assertTrue(captured[0].has_more)
        finally:
            root.destroy()

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

    def test_local_library_deduplicates_exact_content_and_merges_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.jpg"
            duplicate = root / "duplicate.jpg"
            unique = root / "unique.jpg"
            Image.new("RGB", (80, 50), "#66aaff").save(first)
            duplicate.write_bytes(first.read_bytes())
            Image.new("RGB", (80, 50), "#ffaa66").save(unique)
            items = [
                app.Wallpaper("first", str(first), "同图旧记录", False, "旧来源"),
                app.Wallpaper("preferred", str(duplicate), "同图当前记录", True, "新来源"),
                app.Wallpaper("unique", str(unique), "独立图片"),
            ]

            kept, aliases, redundant = app.deduplicate_wallpapers(items, preferred_id="preferred")

            self.assertEqual([item.id for item in kept], ["preferred", "unique"])
            self.assertTrue(kept[0].favorite)
            self.assertEqual(aliases, {"first": "preferred"})
            self.assertEqual(redundant, [first])

    def test_wallpaper_digest_reuses_valid_metadata_and_refreshes_changed_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wallpaper.jpg"
            Image.new("RGB", (80, 50), "#66aaff").save(path)
            item = app.Wallpaper("id", str(path), "缓存哈希")
            first = app.wallpaper_digest(item)
            with patch.object(app, "file_digest", side_effect=AssertionError("valid cache must be reused")):
                self.assertEqual(app.wallpaper_digest(item), first)

            Image.new("RGB", (81, 50), "#ffaa66").save(path)
            refreshed = app.wallpaper_digest(item)
            self.assertNotEqual(refreshed, first)
            self.assertEqual(item.content_size, path.stat().st_size)

    def test_duplicate_cleanup_is_scoped_to_unreferenced_library_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            kept = library / "kept.jpg"
            duplicate = library / "duplicate.jpg"
            outside = root / "outside.jpg"
            for path in (kept, duplicate, outside):
                Image.new("RGB", (40, 30), "#336699").save(path)

            detected = app.find_unreferenced_duplicate_files([kept], library)
            self.assertEqual(detected, [duplicate])

            removed = app.cleanup_redundant_wallpaper_files(
                [duplicate, kept, outside],
                [kept],
                wallpaper_dir=library,
            )

            self.assertEqual(removed, 1)
            self.assertFalse(duplicate.exists())
            self.assertTrue(kept.exists())
            self.assertTrue(outside.exists())

    def test_duplicate_discovery_normalizes_candidate_short_path_aliases(self):
        # Deterministically model Windows resolve expanding RUNNER~1, even when
        # the local volume has 8.3 filename creation disabled.
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp)
            kept, duplicate = library / "kept.jpg", library / "duplicate.jpg"
            for path in (kept, duplicate):
                Image.new("RGB", (40, 30), "#336699").save(path)
            original_resolve = Path.resolve
            def expand_alias(path, *args, **kwargs):
                resolved = original_resolve(path, *args, **kwargs)
                if path.parent == library:
                    return resolved.parent / "expanded-long-directory" / path.name
                return resolved
            with patch.object(Path, "resolve", expand_alias):
                self.assertEqual(app.find_unreferenced_duplicate_files([kept], library), [duplicate])

    def test_duplicate_cleanup_preserves_referenced_and_current_path_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp)
            kept, current, duplicate = (library / name for name in ("kept.jpg", "current.jpg", "duplicate.jpg"))
            for path in (kept, current, duplicate):
                Image.new("RGB", (40, 30), "#336699").save(path)
            kept_alias = library / "KEPT~1.JPG"
            current_alias = library / "CURREN~1.JPG"
            original_resolve = Path.resolve
            aliases = {kept_alias: kept, current_alias: current}
            def expand_alias(path, *args, **kwargs):
                return original_resolve(aliases.get(path, path), *args, **kwargs)
            with patch.object(Path, "resolve", expand_alias):
                removed = app.cleanup_redundant_wallpaper_files(
                    [kept, current, duplicate], [kept_alias], current_alias, library)
            self.assertEqual(removed, 1)
            self.assertTrue(kept.exists())
            self.assertTrue(current.exists())
            self.assertFalse(duplicate.exists())

    def test_duplicate_cleanup_with_real_windows_short_path_when_available(self):
        import ctypes
        with tempfile.TemporaryDirectory(prefix="gallery-long-path-regression-") as temp:
            library = Path(temp) / "wallpaper library long directory"
            library.mkdir()
            kept, current, duplicate = (library / name for name in ("kept.jpg", "current.jpg", "duplicate.jpg"))
            for path in (kept, current, duplicate):
                Image.new("RGB", (40, 30), "#336699").save(path)
            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetShortPathNameW(str(library), buffer, len(buffer))
            if not length or length >= len(buffer) or buffer.value.casefold() == str(library).casefold():
                self.skipTest("8.3 aliases unavailable; deterministic alias regressions remain active")
            short_library = Path(buffer.value)
            detected = app.find_unreferenced_duplicate_files([kept, current], short_library)
            self.assertEqual(detected, [short_library / duplicate.name])
            removed = app.cleanup_redundant_wallpaper_files(
                list(short_library.iterdir()), [kept], current, short_library)
            self.assertEqual(removed, 1)
            self.assertTrue(kept.exists())
            self.assertTrue(current.exists())
            self.assertFalse(duplicate.exists())

    def test_duplicate_cleanup_aborts_when_protected_path_cannot_resolve(self):
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp)
            kept, duplicate = library / "kept.jpg", library / "duplicate.jpg"
            for path in (kept, duplicate):
                Image.new("RGB", (40, 30), "#336699").save(path)
            original_resolve = Path.resolve
            def unavailable(path, *args, **kwargs):
                if path == kept:
                    raise OSError("Cannot resolve protected path")
                return original_resolve(path, *args, **kwargs)
            with patch.object(Path, "resolve", unavailable):
                self.assertEqual(app.find_unreferenced_duplicate_files([kept], library), [])
                self.assertEqual(app.cleanup_redundant_wallpaper_files([duplicate], [kept], wallpaper_dir=library), 0)
                self.assertEqual(app.cleanup_redundant_wallpaper_files([duplicate], [], kept, library), 0)
            self.assertTrue(kept.exists())
            self.assertTrue(duplicate.exists())

    def test_repeated_download_reuses_record_and_apply_replaces_owned_path(self):
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp)
            original = library / "original.jpg"
            downloaded = library / "downloaded.jpg"
            applied = library / "applied.jpg"
            Image.new("RGB", (80, 50), "#88aaff").save(original)
            downloaded.write_bytes(original.read_bytes())
            applied.write_bytes(original.read_bytes())
            existing = app.Wallpaper("existing", str(original), "已有壁纸")
            harness = object.__new__(app.App)
            harness.items = [existing]
            harness._content_index = {app.file_digest(original): existing}

            with patch.object(app, "WALLPAPER_DIR", library):
                stored, added = harness._add_or_merge_wallpaper(
                    app.Wallpaper("download", str(downloaded), "重复下载")
                )
                self.assertFalse(added)
                self.assertIs(stored, existing)
                self.assertEqual(len(harness.items), 1)
                self.assertFalse(downloaded.exists())
                self.assertTrue(original.exists())

                stored, added = harness._add_or_merge_wallpaper(
                    app.Wallpaper("apply", str(applied), "重新应用", source="新转换"),
                    prefer_candidate_path=True,
                )
                self.assertFalse(added)
                self.assertIs(stored, existing)
                self.assertEqual(Path(existing.path), applied)
                self.assertFalse(original.exists())
                self.assertTrue(applied.exists())

    def test_wallpaper_backend_broadcasts_and_waits_for_system_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target.jpg"
            Image.new("RGB", (80, 50), "#88aaff").save(target)
            calls = []
            observed = iter((Path(temp) / "old.jpg", target.resolve()))
            now = [0.0]

            def fake_spi(action, ui_param, value, flags):
                calls.append((action, ui_param, value, flags))
                return True

            result = backend.set_native_wallpaper(
                target,
                confirm_timeout=1.0,
                confirm_interval=0.1,
                current_reader=lambda: next(observed),
                system_parameters_info=fake_spi,
                monotonic=lambda: now[0],
                sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )

            self.assertTrue(result.confirmed)
            self.assertEqual(result.attempts, 2)
            self.assertEqual(calls[0][0], backend.SPI_SETDESKWALLPAPER)
            self.assertEqual(
                calls[0][3],
                backend.SPIF_UPDATEINIFILE | backend.SPIF_SENDCHANGE,
            )

    def test_wallpaper_backend_returns_accepted_unconfirmed_without_false_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target.jpg"
            old = Path(temp) / "old.jpg"
            Image.new("RGB", (80, 50), "#88aaff").save(target)
            now = [0.0]

            result = backend.set_native_wallpaper(
                target,
                confirm_timeout=0.2,
                confirm_interval=0.1,
                current_reader=lambda: old,
                system_parameters_info=lambda *_args: True,
                monotonic=lambda: now[0],
                sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            )

            self.assertFalse(result.confirmed)
            self.assertEqual(result.observed_path, old)
            self.assertGreaterEqual(result.attempts, 2)

    def test_current_wallpaper_prefers_windows_system_api_over_stale_registry(self):
        system_path = Path(r"C:\wallpapers\system-current.jpg")
        with patch.object(
            backend, "_current_wallpaper_from_system_api", return_value=system_path
        ), patch.object(
            backend,
            "_current_wallpaper_from_registry",
            side_effect=AssertionError("registry fallback must not replace system API state"),
        ):
            self.assertEqual(backend.current_native_wallpaper(), system_path)

    def test_wallpaper_apply_completion_resets_button_before_persistence_work(self):
        class FakeButton:
            def __init__(self):
                self.options = {}

            def configure(self, **options):
                self.options.update(options)

        class FakeStatus:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wallpaper.jpg"
            Image.new("RGB", (80, 50), "#88aaff").save(path)
            wallpaper = app.Wallpaper("wallpaper-id", str(path), "收尾测试")
            harness = object.__new__(app.App)
            harness.items = [wallpaper]
            harness.selected_id = wallpaper.id
            harness.online_items = []
            harness.online_selected_id = None
            harness.apply_button = FakeButton()
            harness.online_download_button = FakeButton()
            harness.online_apply_button = FakeButton()
            harness._apply_operation_in_progress = True
            harness._apply_operation_id = "operation"
            harness.config = {}
            harness.status = FakeStatus()
            harness.persist = MagicMock(side_effect=OSError("磁盘写入失败"))
            result = backend.WallpaperApplyResult(path.resolve(), True, path.resolve(), 1)

            with patch.object(app.messagebox, "showinfo"), patch.object(app.messagebox, "showwarning"):
                harness._finish_wallpaper_apply(
                    "operation", wallpaper, result, None, "Windows 原生壁纸", "", None
                )

            self.assertFalse(harness._apply_operation_in_progress)
            self.assertEqual(harness.apply_button.options["state"], "normal")
            self.assertEqual(harness.apply_button.options["text"], "设为壁纸")
            self.assertTrue(path.exists())

    def test_online_apply_commits_asset_before_starting_shared_system_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "downloaded.jpg"
            Image.new("RGB", (80, 50), "#88aaff").save(path)
            wallpaper = app.Wallpaper("wallpaper-id", str(path), "联网资产", source="元气联网下载")
            item = CatalogItem("catalog-id", "联网资产", "https://example.invalid/preview.jpg")
            harness = object.__new__(app.App)
            harness._apply_operation_id = "operation"
            harness._apply_operation_in_progress = True
            harness.selected_id = None
            harness.status = MagicMock()
            harness._add_or_merge_wallpaper = MagicMock(return_value=(wallpaper, True))
            harness.refresh = MagicMock()
            harness._start_wallpaper_apply = MagicMock()

            harness._finish_online_asset_for_apply(
                "operation", item, wallpaper, None
            )

            harness._add_or_merge_wallpaper.assert_called_once_with(wallpaper)
            harness._start_wallpaper_apply.assert_called_once()
            self.assertEqual(harness.selected_id, wallpaper.id)
            self.assertTrue(path.exists())

    def test_system_apply_failure_resets_controls_and_keeps_local_asset(self):
        class FakeButton:
            def __init__(self):
                self.options = {}

            def configure(self, **options):
                self.options.update(options)

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "wallpaper.jpg"
            Image.new("RGB", (80, 50), "#88aaff").save(path)
            wallpaper = app.Wallpaper("wallpaper-id", str(path), "保留测试")
            harness = object.__new__(app.App)
            harness.items = [wallpaper]
            harness.selected_id = wallpaper.id
            harness.online_items = []
            harness.online_selected_id = None
            harness.apply_button = FakeButton()
            harness.online_download_button = FakeButton()
            harness.online_apply_button = FakeButton()
            harness._apply_operation_in_progress = True
            harness._apply_operation_id = "operation"
            harness.config = {}
            harness.status = MagicMock()

            with patch.object(app.messagebox, "showerror"):
                harness._finish_wallpaper_apply(
                    "operation",
                    wallpaper,
                    None,
                    OSError("Windows API 拒绝"),
                    "Windows 原生壁纸",
                    "",
                    None,
                )

            self.assertFalse(harness._apply_operation_in_progress)
            self.assertEqual(harness.apply_button.options["state"], "normal")
            self.assertTrue(path.exists())

    def test_no_desktop_overlay_or_explorer_injection(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                Path(__file__).parents[1] / "app" / "native_wallpaper.py",
                Path(__file__).parents[1] / "app" / "wallpaper_backend.py",
            )
        )
        forbidden = ("WorkerW", "SetParent", "Progman", "FindWindowW", "ShowWindow", "SHELLDLL_DefView")
        for symbol in forbidden:
            self.assertNotIn(symbol, source)
        self.assertIn("SystemParametersInfoW", source)


    def test_resource_source_lists_items_with_session(self):
        def session(): return {"uid": "open-1", "token": "tok-1", "provider": "wechat"}
        def device(): return {"deviceId": "dev-1"}
        source = YuanqiResourceSource(session, device)
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0},
                "data": {"list": [{"id": "491857", "wname": "测试壁纸", "cid": 1, "cname": "风景",
                                   "author": "作者", "jpg_url": "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/static/1.jpg",
                                   "md5": "d41d8cd98f00b204e9800998ecf8427e"}], "total": 100},
            }
            page = source.list_items("1", 1)
        self.assertEqual(len(page.items), 1)
        item = page.items[0]
        self.assertEqual(item.id, "491857")
        self.assertEqual(item.title, "测试壁纸")
        self.assertEqual(item.category_name, "风景")
        self.assertTrue(item.original_url.endswith(".jpg"))

    def test_resource_source_rejects_bad_image_host(self):
        def session(): return {"uid": "open-1", "token": "tok-1"}
        def device(): return {"deviceId": "dev-1"}
        source = YuanqiResourceSource(session, device)
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0},
                "data": {"list": [{"id": "1", "wname": "x", "jpg_url": "https://evil.example/a.jpg"}]},
            }
            with self.assertRaises(CatalogError):
                source.list_items("0", 1)

    def test_resource_source_search_uses_official_global_endpoint(self):
        def session(): return {"uid": "open-1", "token": "tok-1"}
        def device(): return {"deviceId": "dev-1"}
        source = YuanqiResourceSource(session, device)
        items = [
            {"id": "1", "wname": "海边落日", "jpg_url": "https://img-baofun.zhhainiao.com/a.jpg", "cid": 1, "cname": "风景"},
            {"id": "3", "wname": "海边小屋", "jpg_url": "https://img-baofun.zhhainiao.com/c.jpg", "cid": 1, "cname": "风景"},
        ]
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0},
                "data": {"list": items, "total": 2},
            }
            page = source.search("海边")
        url = mock_post.call_args.args[0]
        body = mock_post.call_args.kwargs["json"]
        self.assertTrue(url.endswith("/wallpaper/static/search"))
        self.assertEqual(body["kw"], "海边")
        self.assertEqual((body["page"], body["page_size"]), (1, 24))
        self.assertEqual(len(page.items), 2)
        self.assertEqual(page.total, 2)

    def test_resource_source_featured_uses_index_offset_strategy(self):
        def session(): return {"uid": "open-1", "token": "tok-1"}
        def device(): return {"deviceId": "dev-1"}
        source = YuanqiResourceSource(session, device)
        first = {"id": "310436", "wname": "自然景观", "jpg_url": "https://img-baofun.zhhainiao.com/a.jpg"}
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0}, "data": [first], "total": 501,
            }
            page = source.list_items("__featured__", 2)
        url = mock_post.call_args.args[0]
        body = mock_post.call_args.kwargs["json"]
        self.assertTrue(url.endswith("/wallpaper/static/index"))
        self.assertEqual((body["count"], body["offset"]), (24, 24))
        self.assertEqual(page.items[0].id, "310436")
        self.assertEqual(page.total, 501)

    def test_resource_source_categories_follow_server_order_and_id_kind(self):
        def session(): return {"uid": "open-1", "token": "tok-1"}
        def device(): return {"deviceId": "dev-1"}
        source = YuanqiResourceSource(session, device)
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0},
                "data": [
                    {"tid": 93, "tname": "4k"},
                    {"cid": 1, "cname": "风景"},
                    {"cid": 8, "cname": "小清新"},
                ],
            }
            categories = source.refresh_categories()
        self.assertTrue(mock_post.call_args.args[0].endswith("/wallpaper/static/cate_nav_v2"))
        self.assertEqual(
            [(item.id, item.title) for item in categories],
            [("__following__", "关注"), ("__featured__", "精选"),
             ("tag:93", "4k"), ("cate:1", "风景"), ("cate:8", "小清新")],
        )

    def test_resource_source_category_defaults_to_hot_sort(self):
        def session(): return {"uid": "open-1", "token": "tok-1"}
        def device(): return {"deviceId": "dev-1"}
        source = YuanqiResourceSource(session, device)
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0}, "data": {"list": [], "total": 0},
            }
            source.list_items("tag:93", 1)
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual((body["cate_id"], body["tag_id"], body["sort_type"]), (0, 93, 2))

    def test_dynamic_categories_follow_live_server_order(self):
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}
        )
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0},
                "data": [
                    {"tid": 21283, "tname": "互动"},
                    {"tid": 109, "tname": "4k"},
                    {"cid": 2, "cname": "风景"},
                    {"cid": 24, "tname": "IP专区"},
                ],
            }
            categories = source.refresh_categories()
        self.assertTrue(mock_post.call_args.args[0].endswith("/wallpaper/live/cate_nav_v2"))
        self.assertEqual(
            [(item.id, item.title) for item in categories],
            [
                ("__following__", "关注"),
                ("__featured__", "精选"),
                ("__newest__", "最新"),
                ("tag:21283", "互动"),
                ("tag:109", "4k"),
                ("cate:2", "风景"),
                ("cate:24", "IP专区"),
            ],
        )

    def test_theme_categories_follow_remote_config_order_and_filters(self):
        source = YuanqiThemeResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}
        )
        config = [
            {"cid": 1, "cname": "风景", "cate_ids": [2], "tag_ids": [], "min_mark": 2, "sort_type": 1},
            {"cid": 9, "cname": "随时间变化", "cate_ids": [], "tag_ids": [435499], "min_mark": 2, "sort_type": 1},
        ]
        with patch("catalog.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "resp_common": {"ret": 0}, "data": json.dumps(config, ensure_ascii=False)
            }
            categories = source.refresh_categories()
        self.assertEqual(
            [(item.id, item.title) for item in categories],
            [("__featured__", "精选"), ("theme:1", "风景"), ("theme:9", "随时间变化")],
        )
        self.assertEqual(source._theme_filters["theme:9"]["tag_ids"], [435499])

    def test_theme_category_uses_official_filter_contract_and_parses_components(self):
        converted = ConvertedFrame(b"theme-jpeg", 1920, 1080, 2.0, 50.0, 5)
        converter = unittest.mock.Mock(return_value=converted)
        source = YuanqiThemeResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}, converter=converter
        )
        source._server_categories = (CatalogCategory("theme:1", "风景"),)
        source._theme_filters = {
            "theme:1": {"cate_ids": [2], "tag_ids": [], "min_mark": 2, "sort_type": 1}
        }
        raw = {
            "wid": 58297,
            "wname": "赛博朋克（含时钟、系统信息）",
            "preview_jpg_raw": "https://img-baofun.zhhainiao.com/theme.jpg",
            "preview_video": "https://img-baofun.zhhainiao.com/theme_preview.mp4",
            "video": "https://img-baofun.zhhainiao.com/theme_private.cwp",
            "format": "scene",
            "sub_wtype": 2,
            "theme_type": 1,
            "tag": [{"id": 20275, "tname": "主题"}],
            "resource_detail": json.dumps({"composites": [
                {"type": "mouse", "sub_type": "mouse_tail"},
                {"type": "theme", "sub_type": "theme"},
            ]}),
        }
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0}, "data": {"list": [raw], "total": 25}
            }
            page = source.list_items("theme:1", 1)
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual((body["cate_ids"], body["tag_ids"], body["min_mark"], body["sort_type"]), ([2], [], 2, 1))
        self.assertEqual((body["page"], body["page_size"]), (1, 24))
        self.assertTrue(page.has_more)
        self.assertEqual(page.items[0].resource_kind, "theme")
        self.assertEqual(page.items[0].tags, ("主题",))
        self.assertEqual(page.items[0].components, ("mouse_tail", "theme"))
        self.assertTrue(app.theme_has_live_component_risk(page.items[0]))
        with self.assertRaisesRegex(CatalogError, "禁止自动回退"):
            source.download_original("58297")
        converter.assert_not_called()
        self.assertEqual(source.conversion_summary("58297"), "")

    def test_theme_with_direct_video_uses_dynamic_clean_source_path(self):
        converted = ConvertedFrame(b"clean-theme-jpeg", 3840, 2160, 4.0, 80.0, 5)
        converter = unittest.mock.Mock(return_value=converted)
        source = YuanqiThemeResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}, converter=converter
        )
        item = CatalogItem(
            "theme-clean", "带时钟主题", "https://img-baofun.zhhainiao.com/theme.jpg",
            resource_kind="theme", format="scene",
            source_video_url="https://img-baofun.zhhainiao.com/theme.mp4",
            preview_video_url="https://img-baofun.zhhainiao.com/theme_preview.mp4",
        )
        source._items_by_id[item.id] = item

        self.assertEqual(source.download_original(item.id), b"clean-theme-jpeg")
        converter.assert_called_once_with(item.source_video_url, http=source._http, decrypt=True)
        self.assertTrue(source.conversion_summary(item.id).startswith("高清源 · "))

    def test_theme_featured_combines_carousel_and_cursor_pages(self):
        source = YuanqiThemeResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}
        )

        def raw(wid, title):
            return {
                "wid": wid, "wname": title, "sub_wtype": 2, "theme_type": 1, "format": "scene",
                "preview_jpg_raw": f"https://img-baofun.zhhainiao.com/{wid}.jpg",
                "preview_video": f"https://img-baofun.zhhainiao.com/{wid}_preview.mp4",
            }

        def response_for(url, **kwargs):
            response = unittest.mock.Mock()
            if url.endswith("/wallpaper/live/themes"):
                payload = {"resp_common": {"ret": 0}, "data": [raw(1, "轮播主题")]}
            else:
                cursor = kwargs["json"]["cursor"]
                payload = (
                    {"resp_common": {"ret": 0}, "data": [raw(2, "精选一")], "cursor": "next", "has_more": True}
                    if not cursor
                    else {"resp_common": {"ret": 0}, "data": [raw(3, "精选二")], "cursor": "", "has_more": False}
                )
            response.json.return_value = payload
            return response

        with patch("catalog.requests.post", side_effect=response_for) as mock_post:
            first = source.list_items("__featured__", 1)
            second = source.list_items("__featured__", 2)
        self.assertEqual([item.id for item in first.items], ["1", "2"])
        self.assertEqual([item.id for item in first.featured_items], ["1"])
        self.assertEqual([item.id for item in second.items], ["3"])
        self.assertEqual(second.featured_items, ())
        self.assertTrue(first.has_more)
        self.assertFalse(second.has_more)
        page_requests = [call.kwargs["json"] for call in mock_post.call_args_list if call.args[0].endswith("themes_by_page")]
        self.assertEqual([request["cursor"] for request in page_requests], ["", "next"])

    def test_dynamic_list_prefers_compatible_clean_source_video(self):
        converted = ConvertedFrame(b"jpeg-data", 1920, 1080, 3.3, 100.0, 5)
        converter = unittest.mock.Mock(return_value=converted)
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"},
            lambda: {"deviceId": "d"},
            converter=converter,
        )
        raw = {
            "wid": 730193,
            "wname": "竹林下雨",
            "preview_jpg_raw": "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/live/frame.jpg",
            "preview_jpg_mid": "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/live/frame-mid.jpg",
            "preview_video": "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/preview/demo_preview.mp4",
            "video": "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/live/protected.mp4",
            "resolution": "3840*2160",
        }
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0}, "data": {"list": [raw], "total": 1}
            }
            page = source.list_items("cate:2")
        item = page.items[0]
        self.assertEqual(item.resource_kind, "dynamic")
        self.assertEqual(item.preview_url, raw["preview_jpg_mid"])
        self.assertEqual(item.large_preview_url, raw["preview_jpg_raw"])
        self.assertEqual(item.preview_video_url, raw["preview_video"])
        # 干净高清源视频（XOR 混淆）优先于带 HUD 的 preview_video。
        self.assertEqual(item.source_video_url, raw["video"])
        self.assertEqual(source.download_original(item.id), b"jpeg-data")
        converter.assert_called_once_with(raw["video"], http=source._http, decrypt=True)

    def test_dynamic_preview_falls_back_when_mid_url_is_untrusted(self):
        raw = {
            "wid": 730194,
            "preview_jpg_mid": "http://untrusted.example/frame-mid.jpg",
            "preview_jpg_raw": "https://img-baofun.zhhainiao.com/pcwallpaper_ugc/live/frame.jpg",
        }

        item = YuanqiDynamicResourceSource._parse_item(raw)

        self.assertEqual(item.preview_url, raw["preview_jpg_raw"])
        self.assertEqual(item.large_preview_url, raw["preview_jpg_raw"])

    def test_dynamic_item_ignores_nonstandard_preview_video(self):
        item = YuanqiDynamicResourceSource._parse_item({
            "wid": 730195,
            "preview_jpg": "https://img-baofun.zhhainiao.com/frame.jpg",
            "preview_video": "https://img-baofun.zhhainiao.com/full-video.mp4",
        })

        self.assertEqual(item.preview_video_url, "")

    def test_dynamic_item_accepts_current_yuanqi_preview_cdn(self):
        item = YuanqiDynamicResourceSource._parse_item({
            "wid": 730196,
            "preview_jpg_mid": "https://cdn-img-wallpaper.yuanqidesk3.com/live/frame.jpg",
            "preview_video": "https://cdn-img-wallpaper.yuanqidesk3.com/preview/frame_preview.mp4",
        })

        self.assertTrue(item.preview_url.endswith("frame.jpg"))
        self.assertTrue(item.preview_video_url.endswith("frame_preview.mp4"))

    def test_dynamic_source_failure_falls_back_to_preview_video(self):
        preview = ConvertedFrame(b"preview-jpeg", 1920, 1080, 3.3, 100.0, 5)
        converter = unittest.mock.Mock(side_effect=[ConversionUnavailable("not mp4"), preview])
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}, converter=converter
        )
        item = CatalogItem(
            "1", "测试", "https://img-baofun.zhhainiao.com/frame.jpg",
            resource_kind="dynamic",
            source_video_url="https://img-baofun.zhhainiao.com/source.mp4",
            preview_video_url="https://img-baofun.zhhainiao.com/source_preview.mp4",
        )
        source._items_by_id[item.id] = item

        # 非 scene 动态壁纸没有实时组件风险，正式源失败后应回退标准预览视频。
        self.assertEqual(source.download_original(item.id), b"preview-jpeg")
        self.assertEqual(len(converter.call_args_list), 2)
        self.assertEqual(converter.call_args_list[1].args[0], item.preview_video_url)
        self.assertFalse(converter.call_args_list[1].kwargs["decrypt"])
        self.assertTrue(source.conversion_summary(item.id).startswith("预览回退 · "))

    def test_scene_dynamic_source_failure_is_blocked_without_preview_fallback(self):
        converter = unittest.mock.Mock(side_effect=ConversionUnavailable("CWP3 无法解码"))
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}, converter=converter
        )
        item = CatalogItem(
            "scene-1", "场景测试", "https://img-baofun.zhhainiao.com/frame.jpg",
            resource_kind="dynamic", format="scene",
            source_video_url="https://img-baofun.zhhainiao.com/scene_source.mp4",
            preview_video_url="https://img-baofun.zhhainiao.com/scene_preview.mp4",
        )
        source._items_by_id[item.id] = item

        # scene 且有正式源时必须硬拦，保留 33070dc 的假时钟安全边界。
        with self.assertRaises(CatalogError) as ctx:
            source.download_original(item.id)
        self.assertEqual(len(converter.call_args_list), 1)
        self.assertIn("动态壁纸", str(ctx.exception))
        self.assertIn("禁止自动回退", str(ctx.exception))

    def test_live_component_theme_via_dynamic_source_is_blocked(self):
        converter = unittest.mock.Mock()
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}, converter=converter
        )
        item = CatalogItem(
            "theme-risk", "实时天气主题", "https://img-baofun.zhhainiao.com/frame.jpg",
            resource_kind="theme", format="scene",
            preview_video_url="https://img-baofun.zhhainiao.com/theme_preview.mp4",
        )
        source._items_by_id[item.id] = item

        # 风险主题经全局搜索落到动态源时也必须拦截，避免静默烘焙假时钟。
        with self.assertRaises(CatalogError) as ctx:
            source.download_original(item.id)
        converter.assert_not_called()
        self.assertIn("电脑主题", str(ctx.exception))
        self.assertIn("禁止自动回退", str(ctx.exception))

    def test_plain_theme_via_dynamic_source_falls_back_to_preview(self):
        preview = ConvertedFrame(b"theme-preview-jpeg", 1920, 1080, 2.0, 50.0, 5)
        converter = unittest.mock.Mock(return_value=preview)
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}, converter=converter
        )
        item = CatalogItem(
            "theme-plain", "普通风景主题", "https://img-baofun.zhhainiao.com/frame.jpg",
            resource_kind="theme", format="scene",
            preview_video_url="https://img-baofun.zhhainiao.com/theme_preview.mp4",
        )
        source._items_by_id[item.id] = item

        # 普通主题没有识别出实时组件，仍可安全降级到预览视频。
        self.assertEqual(source.download_original(item.id), b"theme-preview-jpeg")
        converter.assert_called_once_with(item.preview_video_url, http=source._http, decrypt=False)
        self.assertTrue(source.conversion_summary(item.id).startswith("预览视频 · "))

    def test_dynamic_private_container_source_is_skipped_before_converter(self):
        for suffix in (".cwp", ".zip"):
            with self.subTest(suffix=suffix):
                preview = ConvertedFrame(b"preview-jpeg", 1920, 1080, 3.3, 100.0, 5)
                converter = unittest.mock.Mock(return_value=preview)
                source = YuanqiDynamicResourceSource(
                    lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}, converter=converter
                )
                item = CatalogItem(
                    f"private{suffix}", "测试", "https://img-baofun.zhhainiao.com/frame.jpg",
                    resource_kind="dynamic",
                    source_video_url=f"https://img-baofun.zhhainiao.com/scene{suffix}",
                    preview_video_url="https://img-baofun.zhhainiao.com/scene_preview.mp4",
                )
                source._items_by_id[item.id] = item

                # 私有容器提前跳过，避免白下载几十 MiB；非 scene 仍可回退预览视频。
                self.assertEqual(source.download_original(item.id), b"preview-jpeg")
                converter.assert_called_once_with(item.preview_video_url, http=source._http, decrypt=False)
                self.assertTrue(source.conversion_summary(item.id).startswith("预览回退 · "))

    def test_dynamic_global_search_uses_official_endpoint(self):
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}
        )
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0}, "data": {"list": [], "total": 0}
            }
            source.search("山野")
        url = mock_post.call_args.args[0]
        body = mock_post.call_args.kwargs["json"]
        self.assertTrue(url.endswith("/v20526/wplive/opensearch/with/ptype"))
        self.assertEqual(body["kw"], "山野")
        self.assertEqual((body["page"], body["page_size"]), (1, 24))

    def test_dynamic_following_does_not_repeat_server_collection_as_page_two(self):
        source = YuanqiDynamicResourceSource(
            lambda: {"uid": "u", "token": "t"}, lambda: {"deviceId": "d"}
        )
        with patch("catalog.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0}, "data": {"list": [], "total": 0}
            }
            first = source.list_items("__following__", 1)
            second = source.list_items("__following__", 2)
        self.assertFalse(first.has_more)
        self.assertEqual(second.items, ())
        self.assertEqual(mock_post.call_count, 1)

    def test_session_store_round_trip_encrypted(self):
        with tempfile.TemporaryDirectory() as temp:
            original = session_store.SESSION_PATH
            session_store.SESSION_PATH = Path(temp) / "session.enc"
            try:
                session_store.save_session("tok-secret", "open-1", "dev-1")
                self.assertEqual(session_store.load_session(), ("tok-secret", "open-1", "dev-1"))
                cipher = session_store.SESSION_PATH.read_bytes()
                self.assertNotIn(b"tok-secret", cipher)
                session_store.clear_session()
                self.assertIsNone(session_store.load_session())
            finally:
                session_store.SESSION_PATH = original

    def test_autostart_command_uses_pythonw_and_script(self):
        command = autostart_command()
        self.assertIn("pythonw.exe", command)
        self.assertIn("native_wallpaper.py", command)
        self.assertTrue(command.startswith('"') and command.endswith('"'))

    def test_single_instance_mutex(self):
        # 用独立命名的互斥体，避免与正在运行的应用实例冲突。
        first = SingleInstance("Local\\TestNativeWallpaperMutex")
        second = SingleInstance("Local\\TestNativeWallpaperMutex")
        try:
            self.assertFalse(first.already_running)
            self.assertTrue(second.already_running)
        finally:
            first.release()
            second.release()

    def test_autostart_registry_toggle(self):
        # Isolate the real registry exercise from the user's actual startup entry.
        import product
        import winreg
        import uuid
        key_path = "Software\\NativeWallpaperManagerTests\\" + uuid.uuid4().hex
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path):
            pass
        try:
            with patch.object(product, "RUN_KEY", key_path):
                self.assertTrue(set_autostart(True))
                self.assertTrue(autostart_enabled())
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    self.assertEqual(winreg.QueryValueEx(key, product.APP_NAME)[0], autostart_command())
                self.assertTrue(set_autostart(False))
                self.assertFalse(autostart_enabled())
        finally:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)

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
