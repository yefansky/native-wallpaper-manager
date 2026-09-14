"""自研客户端的微信扫码登录窗口（独立子进程）。

用 pywebview 打开元气官方公开登录页 `https://newvip.ijinshan.com/#/auth/3`，
路由守卫会整页跳转到微信官方 `open.weixin.qq.com/connect/qrconnect` 二维码页。
由叶帆本人用微信扫码并在手机端确认后，页面自行用 code 调
`account-cloud.duba.net/api/v1/user/login` 换取会话（token + user_info），并把
会话写入页面 cookie。本窗口读取内存中的会话，用
`mobile-wallpaper-api.zhhainiao.com/v1/user/info` 做服务端校验，然后把会话
结构通过 stdout 返回给桌面画廊主进程。

安全边界：
- 令牌、open_id、device_id 只在进程内存中传递；结果只输出到 stdout 管道，
  不写磁盘、不进日志、不进 Git。父进程拿到后立即清空管道缓冲。
- 不读取/复用官方客户端的 Cookie、令牌或身份；本会话由本次微信扫码重新签发。
- 不绕过微信扫码确认、TLS 或会员权限；用户扫码与确认必须由本人完成。
- 二维码内容不落盘、不解析。

用法：
    python app/wechat_login_window.py            # 登录成功后 stdout 打一行 JSON
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.parse
from typing import Any

import requests
import webview

from wechat_login import RedactedMapping, log_diag, sha8

LOGIN_PAGE = "https://newvip.ijinshan.com/#/auth/3"
EXCHANGE_URL = "https://account-cloud.duba.net/api/v1/user/login"
USER_INFO_URL = "https://account-cloud.duba.net/api/v1/user/userInfo"
VERIFY_URL = "https://mobile-wallpaper-api.zhhainiao.com/v1/user/info"
POLL_INTERVAL = 0.8
LOGIN_TIMEOUT = 600  # 10 分钟：等本人扫码、手机确认并回流
PLAT = "170"  # 微信
DEBUG = "--debug" in sys.argv[1:]


def _debug(line: str) -> None:
    if DEBUG:
        print(f"[login-debug] {line}", file=sys.stderr, flush=True)


def build_exchange_body(wechat_code: str) -> dict[str, Any]:
    return RedactedMapping(reqestType=1, token=wechat_code, plat=PLAT, tel="", code="")


def exchange_code(wechat_code: str) -> tuple[str, str] | None:
    """用微信 code 向金山账号服务换会话；返回 (token, open_id) 或 None。"""
    try:
        response = requests.post(EXCHANGE_URL, json=build_exchange_body(wechat_code), timeout=15)
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    if payload.get("resp_common", {}).get("ret") != 0:
        return None
    data = payload.get("data") or {}
    token = str(data.get("token") or "")
    user_info = data.get("user_info") or {}
    open_id = str(user_info.get("open_id") or "")
    if token and open_id:
        return token, open_id
    return None


def resolve_open_id(token: str, retries: int = 3) -> str:
    """用 token 调 /api/v1/user/userInfo 取真实账号 open_id；带重试防瞬时失败。"""
    for attempt in range(retries):
        try:
            response = requests.post(USER_INFO_URL, json=RedactedMapping(token=token), timeout=15)
            payload = response.json()
            ret = payload.get("resp_common", {}).get("ret")
            if ret == 0:
                data = payload.get("data") or {}
                open_id = str(data.get("open_id") or "")
                uid = str(data.get("uid") or "")
                nickname = str(data.get("nickname") or "")
                log_diag(
                    f"resolve_open_id ok attempt={attempt} open_id={sha8(open_id)} uid={sha8(uid)} "
                    f"nickname={nickname} keys={sorted(data.keys())}"
                )
                # 优先返回与资源服务一致的账号 uid；open_id 可能随登录方式变化。
                return uid or open_id
            log_diag(f"resolve_open_id ret={ret} attempt={attempt}")
        except (requests.RequestException, ValueError) as exc:
            log_diag(f"resolve_open_id error={exc!r} attempt={attempt}")
        time.sleep(1.0)
    return ""


def verify_session(open_id: str, token: str, device_id: str) -> bool:
    """用资源服务校验会话；ret==0 表示服务端接受该会话。"""
    return verify_session_detail(open_id, token, device_id)[0]


def verify_session_detail(open_id: str, token: str, device_id: str) -> tuple[bool, int | None]:
    """返回 (是否通过, 资源服务 ret 码)。"""
    body = {"common": {"platform": "pc", "open_id": open_id, "token": token, "device_id": device_id}}
    try:
        response = requests.post(VERIFY_URL, json=body, timeout=15)
        payload = response.json()
        ret = payload.get("resp_common", {}).get("ret")
        log_diag(f"window verify open_id={sha8(open_id)} token_len={len(token)} ret={ret} msg={str(payload.get('resp_common', {}).get('msg'))[:40]}")
        return ret == 0, ret
    except (requests.RequestException, ValueError) as exc:
        log_diag(f"window verify error={exc!r}")
        return False, None


class BridgeApi:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def on_bridge(self, cmd: str, payload: str) -> str:
        # payload 里可能含本次会话令牌，只驻留本进程内存，供 _poll 解析。
        self.calls.append({"cmd": cmd, "payload": payload or ""})
        return "{}"


class LoginWindow:
    def __init__(self) -> None:
        self.api = BridgeApi()
        self.window: webview.Window | None = None
        self.session: dict[str, str] | None = None
        self.error: str | None = None
        self._cookie_seen = ""

    def _inject_bridge_js(self) -> str:
        return (
            "(function(){"
            "try{"
            "window.external.CallWrapper=function(cmd,payload){"
            "  try{ window.pywebview.api.on_bridge(String(cmd), String(payload)); }catch(e){}"
            "  return '{}';"
            "};"
            "}catch(e){ return 'inject-error:'+String(e); }"
            "return 'ok';"
            "})()"
        )

    def _read_session_js(self) -> str:
        return (
            "(function(){"
            "var out={url:location.href, cookie:document.cookie};"
            "return JSON.stringify(out);"
            "})()"
        )

    def _ui_cookie_open_id(self) -> str:
        """从页面 UI cookie（登录响应 user_info）读取资源服务认可的账号 open_id。"""
        try:
            raw = self.window.evaluate_js(self._read_session_js())
            state = json.loads(raw or "{}")
            cookie = state.get("cookie", "")
            user_json = urllib.parse.unquote(self._cookie_value(cookie, "UI") or "")
            user_info = json.loads(user_json)
            open_id = str(user_info.get("open_id") or "")
            log_diag(
                f"UI cookie user_info keys={sorted(user_info.keys())} open_id={sha8(open_id)} "
                f"nickname={str(user_info.get('nickname'))[:20]}"
            )
            return open_id
        except Exception as exc:  # noqa: BLE001
            log_diag(f"UI cookie 解析失败 {exc!r}")
            return ""

    def _poll(self) -> None:
        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                if self._poll_once():
                    return
            except Exception as exc:  # noqa: BLE001
                _debug(f"poll 轮询单次异常 {exc!r}，继续")
        self.error = "等待微信扫码登录超时。"
        self._close()

    def _poll_once(self) -> bool:
        """单次轮询；返回 True 表示已取得会话并关闭窗口。"""
        # 1) 优先从原生桥回调取会话（页面登录成功后调 CallWrapper('winClose', {token, open_id, type})）。
        for call in self.api.calls:
            if call["cmd"] != "winClose":
                continue
            try:
                payload = json.loads(call["payload"])
            except (ValueError, TypeError):
                continue
            token = str(payload.get("token") or "")
            if not token:
                continue
            raw_open_id = str(payload.get("open_id") or "")
            log_diag(f"winClose 原始 open_id={sha8(raw_open_id)} token_len={len(token)}")
            # 先用会话 open_id 占位；主 open_id 在 run() 里等账号服务稳定后再解析并校验。
            _debug(f"winClose 捕获 token_len={len(token)}")
            self.session = {"token": token, "open_id": raw_open_id}
            self._close()
            return True
        # 2) 从页面 cookie 读会话：AT=token, UI=user_info(JSON)。
        try:
            raw = self.window.evaluate_js(self._read_session_js())
            state = json.loads(raw or "{}")
        except Exception as exc:  # noqa: BLE001
            _debug(f"evaluate_js 异常 {exc!r}")
            return False
        if not state:
            return False
        url = state.get("url", "")
        cookie = state.get("cookie", "")
        cookie_names = [p.split("=", 1)[0] for p in cookie.split(";") if "=" in p]
        _debug(f"url={url!r} cookie_names={cookie_names} bridge_cmds={[c['cmd'] for c in self.api.calls]}")
        if cookie and cookie != self._cookie_seen:
            self._cookie_seen = cookie
        token = self._cookie_value(cookie, "AT")
        if token:
            _debug(f"AT cookie 存在 token_len={len(token)}")
            # 优先用账号服务解析主 open_id（资源服务认可）；UI cookie 是会话 open_id。
            open_id = resolve_open_id(token)
            if not open_id:
                user_json = urllib.parse.unquote(self._cookie_value(cookie, "UI") or "")
                try:
                    user_info = json.loads(user_json)
                    open_id = str(user_info.get("open_id") or "")
                    log_diag(f"UI cookie user_info keys={sorted(user_info.keys())} open_id={sha8(open_id)} nickname={str(user_info.get('nickname'))[:20]}")
                except (ValueError, TypeError):
                    log_diag("UI cookie 解析失败")
            if not open_id:
                code = self._query_param(url, "code")
                if code:
                    exchanged = exchange_code(code)
                    if exchanged:
                        token, open_id = exchanged
            if token and open_id:
                self.session = {"token": token, "open_id": open_id}
                self._close()
                return True
        # 3) 兜底：页面 URL 带 code 且 cookie 未更新时自行兑换（页面可能已消耗 code）。
        code = self._query_param(url, "code")
        if code and "AT" not in cookie:
            _debug("URL 带 code，尝试自行兑换")
            exchanged = exchange_code(code)
            if exchanged:
                token, open_id = exchanged
                self.session = {"token": token, "open_id": open_id}
                self._close()
                return True
        return False

    def _close(self) -> None:
        try:
            if self.window:
                self.window.destroy()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _cookie_value(cookie: str, name: str) -> str:
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    @staticmethod
    def _query_param(url: str, name: str) -> str:
        if not url:
            return ""
        parsed = urllib.parse.urlparse(url)
        # SPA 用 hash 路由，code 可能在 fragment 里。
        query = parsed.query or (parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else "")
        return urllib.parse.parse_qs(query).get(name, [""])[0]


def run(smoke: bool = False) -> int:
    window_app = LoginWindow()
    window = webview.create_window(
        "微信扫码登录 · 桌面画廊",
        LOGIN_PAGE,
        width=430,
        height=600,
        js_api=window_app.api,
        hidden=smoke,
    )
    window_app.window = window

    def inject() -> None:
        try:
            window.evaluate_js(window_app._inject_bridge_js())
        except Exception:  # noqa: BLE001
            pass

    window.events.loaded += inject
    if smoke:
        # 冒烟模式：打开微信二维码页后自动关闭，只验证二维码能显示。
        def smoke_report() -> None:
            try:
                url = window.get_current_url()
                body = window.evaluate_js("document.body?document.body.innerText.slice(0,80):''")
                print(json.dumps({"ok": True, "qr_page": "open.weixin.qq.com" in url, "url": url,
                                  "body_head": (body or "")[:60]}, ensure_ascii=True), flush=True)
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"ok": False, "message": repr(exc)}, ensure_ascii=True), flush=True)
            finally:
                try:
                    window.destroy()
                except Exception:
                    pass

        threading.Timer(5.0, smoke_report).start()
        webview.start(gui="edgechromium")
        return 0

    threading.Thread(target=window_app._poll, daemon=True).start()
    webview.start(gui="edgechromium")

    if window_app.error:
        print(json.dumps({"ok": False, "message": window_app.error}, ensure_ascii=True), flush=True)
        return 1
    if not window_app.session:
        print(json.dumps({"ok": False, "message": "未取得会话。"}, ensure_ascii=True), flush=True)
        return 1

    token = window_app.session["token"]
    device_id = __import__("uuid").uuid4().hex
    # 登录刚完成后，账号服务需要几秒才把 token 关联到主账号 open_id；
    # 轮询 resolve+verify，直到资源服务 ret==0。
    verified, resource_ret = False, None
    resolved_open_id = ""
    for attempt in range(15):
        resolved_open_id = resolve_open_id(token)
        if resolved_open_id:
            verified, resource_ret = verify_session_detail(resolved_open_id, token, device_id)
            if verified:
                break
        log_diag(f"会话校验第 {attempt + 1} 次未通过，2 秒后重试")
        time.sleep(2)
    session_open_id = window_app.session["open_id"]
    result = {
        "ok": verified,
        "verified": verified,
        "diagnostics": {
            "session_open_id": sha8(session_open_id),
            "resolved_open_id": sha8(resolved_open_id),
            "resource_ret": resource_ret,
        },
    }
    if verified:
        result["session"] = {
            "open_id": resolved_open_id,
            "token": token,
            "device_id": device_id,
        }
    else:
        result["message"] = "扫码成功但资源服务未认可该会话（账号可能未绑定资源服务，或需稍后再试）。"
        result["session"] = {
            "open_id": session_open_id,
            "token": token,
            "device_id": device_id,
        }
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if verified else 1


def smoke_package() -> int:
    """Exercise the frozen WebView2 bridge without network or login state."""
    result = {"ok": False, "message": "WebView2 smoke timed out"}
    finished = threading.Event()
    window = webview.create_window(
        "Desktop Gallery package test",
        html='<html><body><div id="package-check">ready</div></body></html>',
        hidden=True,
    )

    def check() -> None:
        try:
            value = window.evaluate_js("document.getElementById('package-check').textContent")
            result.update(ok=value == "ready", message="WebView2 JavaScript bridge")
        except Exception as exc:
            result.update(ok=False, message=repr(exc))
        finally:
            finished.set()
            window.destroy()

    def timeout() -> None:
        if not finished.is_set():
            window.destroy()

    window.events.loaded += check
    timer = threading.Timer(15.0, timeout)
    timer.daemon = True
    timer.start()
    try:
        webview.start(gui="edgechromium", private_mode=True)
    except Exception as exc:
        result.update(ok=False, message=repr(exc))
    finally:
        timer.cancel()
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if "--smoke-package" in sys.argv[1:]:
        sys.exit(smoke_package())
    sys.exit(run(smoke="--smoke" in sys.argv[1:]))
