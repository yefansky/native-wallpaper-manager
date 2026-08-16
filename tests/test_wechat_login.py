import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
from wechat_login import (
    LoginProtocolPending,
    PollResult,
    QrCode,
    WechatLoginEndpoints,
    WechatQrLoginBackend,
    WechatQrStatus,
)
from wechat_login_window import LoginWindow, build_exchange_body, exchange_code, resolve_open_id
from yuanqi_auth import AuthPhase, YuanqiAuthCoordinator


class WechatLoginTests(unittest.TestCase):
    def test_empty_endpoints_are_not_captured(self):
        endpoints = WechatLoginEndpoints()
        self.assertFalse(endpoints.captured)

    def test_filled_endpoints_are_captured(self):
        endpoints = WechatLoginEndpoints(qr_url="https://gw.example/qr", poll_url="https://gw.example/poll")
        self.assertTrue(endpoints.captured)

    def test_with_captured_returns_new_filled_instance(self):
        base = WechatLoginEndpoints()
        filled = base.with_captured(qr_url="https://gw.example/qr", poll_url="https://gw.example/poll")
        self.assertTrue(filled.captured)
        self.assertFalse(base.captured)
        self.assertEqual(filled.qr_url, "https://gw.example/qr")

    def test_backend_unconfigured_refuses_requests(self):
        backend = WechatQrLoginBackend()
        self.assertFalse(backend.configured())
        with self.assertRaises(LoginProtocolPending):
            backend.navigate_to_login()
        with self.assertRaises(LoginProtocolPending):
            backend.poll_login_status()
        with self.assertRaises(LoginProtocolPending):
            backend.request_phone_code("13800000000")

    def test_backend_generates_fresh_device_id(self):
        left = WechatQrLoginBackend().device_id
        right = WechatQrLoginBackend().device_id
        self.assertNotEqual(left, right)
        self.assertEqual(len(left), 32)

    def test_qr_repr_redacts_ticket_and_image(self):
        qr = QrCode("ticket-secret", "https://img.example/qr.png")
        self.assertNotIn("ticket-secret", repr(qr))
        self.assertNotIn("qr.png", repr(qr))
        self.assertEqual(len(qr.fingerprint), 10)

    def test_poll_result_redacts_token(self):
        result = PollResult(WechatQrStatus.CONFIRMED, token="do-not-print", open_id="user-1")
        self.assertNotIn("do-not-print", repr(result))

    def test_coordinator_stays_unavailable_until_protocol_captured(self):
        auth = YuanqiAuthCoordinator(WechatQrLoginBackend())
        self.assertEqual(auth.begin_login().phase, AuthPhase.UNAVAILABLE)
        self.assertFalse(auth.snapshot.has_session)

    def test_store_session_then_coordinator_reaches_authenticated(self):
        backend = WechatQrLoginBackend()
        backend.store_session("token-secret", "open-id-1", "device-1")
        self.assertEqual(backend.get_user_info()["uid"], "open-id-1")
        self.assertEqual(backend.get_device_info()["deviceId"], "device-1")
        auth = YuanqiAuthCoordinator(backend)
        with patch("wechat_login.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"resp_common": {"ret": 0}}
            snapshot = auth.callback_on_login({"ret": 0})
        self.assertEqual(snapshot.phase, AuthPhase.AUTHENTICATED)
        self.assertTrue(snapshot.has_session)

    def test_verify_session_accepts_only_ret_zero(self):
        backend = WechatQrLoginBackend()
        from yuanqi_auth import YuanqiSession
        session = YuanqiSession("open-id-1", "device-1", "token-secret", "wechat")
        with patch("wechat_login.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"resp_common": {"ret": 0}}
            self.assertTrue(backend.verify_session(session))
        with patch("wechat_login.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"resp_common": {"ret": 4001004}}
            self.assertFalse(backend.verify_session(session))
        with patch("wechat_login.requests.post", side_effect=OSError("network down")):
            self.assertFalse(backend.verify_session(session))

    def test_token_not_persisted_in_backend_state(self):
        backend = WechatQrLoginBackend()
        backend.store_session("super-secret-token", "open-id-1", "device-1")
        self.assertNotIn("super-secret-token", repr(backend))
        self.assertNotIn("super-secret-token", repr(backend.get_user_info()))

    def test_exchange_body_uses_wechat_plat(self):
        body = build_exchange_body("wx-code-123")
        self.assertEqual(body["reqestType"], 1)
        self.assertEqual(body["plat"], "170")
        self.assertEqual(body["token"], "wx-code-123")
        self.assertNotIn("wx-code-123", repr(body))

    def test_exchange_code_parses_session(self):
        with patch("wechat_login_window.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0},
                "data": {"token": "tok-1", "user_info": {"open_id": "oid-1"}},
            }
            self.assertEqual(exchange_code("code"), ("tok-1", "oid-1"))
        with patch("wechat_login_window.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"resp_common": {"ret": 4001001}}
            self.assertIsNone(exchange_code("code"))

    def test_login_window_cookie_helpers(self):
        window = LoginWindow()
        self.assertEqual(window._cookie_value("AT=abc; UI=def", "AT"), "abc")
        self.assertEqual(window._cookie_value("AT=abc; UI=def", "UI"), "def")
        self.assertEqual(window._cookie_value("AT=abc", "MISSING"), "")
        self.assertEqual(window._query_param("https://x/#/auth/3?code=wx123&x=1", "code"), "wx123")
        self.assertEqual(window._query_param("https://x/", "code"), "")

    def test_resolve_open_id_prefers_account_service(self):
        with patch("wechat_login_window.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {
                "resp_common": {"ret": 0},
                "data": {"open_id": "real-account-uid", "nickname": "叶少"},
            }
            self.assertEqual(resolve_open_id("tok"), "real-account-uid")
        with patch("wechat_login_window.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"resp_common": {"ret": 4001002}}
            self.assertEqual(resolve_open_id("tok", retries=1), "")

    def test_winclose_captures_token_with_placeholder_open_id(self):
        window = LoginWindow()
        window.window = None
        window.api.calls.append({"cmd": "winClose", "payload": '{"token":"tok-9","open_id":"session-id","type":1}'})
        self.assertTrue(window._poll_once())
        # 主 open_id 由 run() 在账号服务稳定后解析；winClose 阶段先占位。
        self.assertEqual(window.session, {"token": "tok-9", "open_id": "session-id"})


if __name__ == "__main__":
    unittest.main()
