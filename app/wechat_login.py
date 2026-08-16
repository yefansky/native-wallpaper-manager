"""自研微信扫码登录后端。

自研客户端内嵌官方公开登录页 `https://newvip.ijinshan.com/#/auth/3`，由叶帆本人
用微信扫码；登录页自行用 code 调 `account-cloud.duba.net/api/v1/user/login` 换取
会话 token。本模块持有会话，用资源服务 `mobile-wallpaper-api.zhhainiao.com/v1/user/info`
校验（`ret==0` 才算登录成功），并供桌面画廊协调器驱动登录状态机。

安全边界：本模块只使用自己生成的设备标识，不读取/复制官方客户端的设备值、令牌
或身份配置；令牌只在内存中传递，`repr` 不显示，不写配置、不进 Git；诊断日志只
保留长度、状态码、不可逆摘要。
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import requests

DEFAULT_QR_FIELD = "qr"
DEFAULT_TICKET_FIELD = "ticket"
DEFAULT_STATUS_FIELD = "status"
DEFAULT_TOKEN_FIELD = "token"
DEFAULT_OPENID_FIELD = "open_id"
VERIFY_URL = "https://mobile-wallpaper-api.zhhainiao.com/v1/user/info"

_LOGIN_DEBUG_DIR = Path.home() / "AppData" / "Local" / "NativeWallpaperManager"


def log_diag(line: str) -> None:
    """写脱敏诊断行；只含长度、状态码、摘要，绝不写令牌/账号值。"""
    try:
        _LOGIN_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        with (_LOGIN_DEBUG_DIR / "login_debug.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def sha8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


class RedactedMapping(dict):
    """dict 的 repr 会隐藏 `token` 键的值，避免令牌出现在日志/异常文本。"""

    REDACTED_KEYS = ("token",)

    def __repr__(self) -> str:
        items = ", ".join(
            f"{key!r}: {('<redacted>' if key in self.REDACTED_KEYS else value)!r}"
            for key, value in self.items()
        )
        return f"{{{items}}}"


class LoginProtocolPending(RuntimeError):
    """真实登录协议端点尚未捕获，拒绝发起请求。"""

    REASON = "登录网关端点尚未捕获；先用 tools/cdp_capture.py 捕获一次本人扫码登录后填入 WechatLoginEndpoints。"


class WechatQrStatus:
    """二维码轮询的状态；数值与字段名在捕获后按真实服务端对齐。"""

    WAITING = "waiting"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass(frozen=True)
class WechatLoginEndpoints:
    """从一次真实登录捕获中提取的协议结构（只含字段名与模板，不含任何值）。

    端点模板用 `{name}` 占位符引用本机生成的非秘密参数；捕获完成前均为空。
    """

    qr_url: str = ""
    qr_method: str = "GET"
    qr_param_names: tuple[str, ...] = ()
    qr_ticket_field: str = DEFAULT_TICKET_FIELD
    qr_image_field: str = DEFAULT_QR_FIELD

    poll_url: str = ""
    poll_method: str = "GET"
    poll_param_names: tuple[str, ...] = ()
    poll_status_field: str = DEFAULT_STATUS_FIELD

    confirmed_token_field: str = DEFAULT_TOKEN_FIELD
    confirmed_openid_field: str = DEFAULT_OPENID_FIELD

    @property
    def captured(self) -> bool:
        return bool(self.qr_url and self.poll_url)

    def with_captured(self, **overrides: object) -> "WechatLoginEndpoints":
        """捕获到真实端点后，返回补齐字段的新实例。"""
        data = {
            "qr_url": self.qr_url,
            "qr_method": self.qr_method,
            "qr_param_names": self.qr_param_names,
            "qr_ticket_field": self.qr_ticket_field,
            "qr_image_field": self.qr_image_field,
            "poll_url": self.poll_url,
            "poll_method": self.poll_method,
            "poll_param_names": self.poll_param_names,
            "poll_status_field": self.poll_status_field,
            "confirmed_token_field": self.confirmed_token_field,
            "confirmed_openid_field": self.confirmed_openid_field,
        }
        data.update(overrides)
        return WechatLoginEndpoints(**data)


@dataclass(frozen=True)
class QrCode:
    """二维码与一次性轮询标识；只驻留内存，不落盘。"""

    ticket: str = field(repr=False)
    image_url: str = field(repr=False)
    status: str = WechatQrStatus.WAITING

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.image_url.encode("utf-8", "replace")).hexdigest()
        return digest[:10]


@dataclass(frozen=True)
class PollResult:
    status: str
    message: str = ""
    token: str = field(default="", repr=False)
    open_id: str = field(default="", repr=False)

    @property
    def confirmed(self) -> bool:
        return self.status == WechatQrStatus.CONFIRMED


class WechatQrLoginBackend:
    """由本进程发起的微信扫码登录后端。

    在端点捕获前，`configured()` 返回 False；`navigate_to_login()` 与
    `poll_login_status()` 会抛出 `LoginProtocolPending`。捕获后填入端点即可
    让 `YuanqiAuthCoordinator` 驱动完整登录状态链。
    """

    def __init__(self, endpoints: WechatLoginEndpoints | None = None):
        self._endpoints = endpoints or WechatLoginEndpoints()
        self._device_id = uuid.uuid4().hex  # 只使用本机新生成的设备标识。
        self._qr: QrCode | None = None
        self._session_data: dict[str, str] = {}

    def configured(self) -> bool:
        return self._endpoints.captured

    @property
    def endpoints(self) -> WechatLoginEndpoints:
        return self._endpoints

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def current_qr(self) -> QrCode | None:
        return self._qr

    def navigate_to_login(self) -> QrCode:
        """向登录网关申请二维码；返回可展示的 QrCode。"""
        if not self.configured():
            raise LoginProtocolPending(LoginProtocolPending.REASON)
        # 捕获端点后：用 self._device_id 与 qr_param_names 构造请求，
        # 解析 qr_ticket_field / qr_image_field，构造并缓存 QrCode。
        raise NotImplementedError("捕获登录网关端点后实现二维码请求。")

    def poll_login_status(self) -> PollResult:
        """用 ticket 轮询扫码状态；确认后返回令牌与 open_id。"""
        if not self.configured():
            raise LoginProtocolPending(LoginProtocolPending.REASON)
        if self._qr is None:
            raise RuntimeError("尚未申请二维码。")
        # 捕获端点后：用 self._qr.ticket 构造轮询请求，解析状态字段；
        # CONFIRMED 时把 confirmed_token_field / confirmed_openid_field 存入内存。
        raise NotImplementedError("捕获登录网关端点后实现扫码轮询。")

    def request_phone_code(self, phone_number: str) -> None:
        raise LoginProtocolPending("本后端只实现微信扫码；手机登录需要独立端点。")

    def submit_phone_login(self, phone_number: str, verification_code: str) -> None:
        raise LoginProtocolPending("本后端只实现微信扫码；手机登录需要独立端点。")

    def store_session(self, token: str, open_id: str, device_id: str) -> None:
        """把登录窗口取得的会话放入内存，供协调器读取与校验。"""
        log_diag(f"store_session open_id={sha8(open_id)} token_len={len(token)} device={sha8(device_id)}")
        self._session_data = RedactedMapping(uid=open_id, token=token, provider="wechat")
        self._device_id = device_id

    def get_user_info(self) -> Mapping[str, object]:
        if not self._session_data:
            raise RuntimeError("登录尚未完成。")
        return RedactedMapping(self._session_data)

    def get_device_info(self) -> Mapping[str, object]:
        return {"deviceId": self._device_id}

    def verify_session(self, session: object) -> bool:
        """用资源服务校验会话；只有服务端 ret==0 才算真登录。"""
        token = getattr(session, "token", "")
        uid = getattr(session, "uid", "")
        device_id = getattr(session, "device_id", "")
        body = {"common": {"platform": "pc", "open_id": uid, "token": token, "device_id": device_id}}
        try:
            response = requests.post(VERIFY_URL, json=body, timeout=15)
            payload = response.json()
            ret = payload.get("resp_common", {}).get("ret")
            log_diag(
                f"verify_session open_id={sha8(uid)} token_len={len(token)} "
                f"device={sha8(device_id)} ret={ret} msg={str(payload.get('resp_common', {}).get('msg'))[:40]}"
            )
            return ret == 0
        except (OSError, ValueError) as exc:
            log_diag(f"verify_session network_error={exc!r}")
            return False

    def clear(self) -> None:
        self._qr = None
        self._session_data.clear()
