"""元气桌面原生登录桥的本地契约与状态机。

官方 H5 只负责调用原生宿主桥；真正的 QQ、微信或手机号认证由官方 PC
宿主完成。本模块复刻可验证的桥方法、回调和登录状态流，但不内置官方客户端
身份，也不读取、复制或持久化官方客户端的会话秘密。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Protocol


@dataclass(frozen=True)
class BridgeMethod:
    channel: str
    callback: str = ""
    timeout_ms: int | None = None


GET_USER_INFO = BridgeMethod("getUserInfo", "callbackUserInfo", 20_000)
GET_DEVICE_INFO = BridgeMethod("getDeviceInfo", "callbackDeviceInfo")
LOGIN = BridgeMethod("navigate2Login", "callbackOnLogin")
BRIDGE_METHODS = (LOGIN, GET_USER_INFO, GET_DEVICE_INFO)


class LoginProvider(str, Enum):
    WECHAT = "wechat"
    PHONE = "phone"
    QQ = "qq"


class LoginInteraction(str, Enum):
    QR_SCAN = "qr_scan"
    NATIVE_FORM = "native_form"
    NATIVE_HANDOFF = "native_handoff"


@dataclass(frozen=True)
class LoginOption:
    """官方电脑版快速登录窗中可见的认证入口。"""

    provider: LoginProvider
    label: str
    interaction: LoginInteraction
    instruction: str


LOGIN_OPTIONS = (
    LoginOption(LoginProvider.WECHAT, "微信登录", LoginInteraction.QR_SCAN, "微信扫一扫登录"),
    LoginOption(LoginProvider.PHONE, "手机登录", LoginInteraction.NATIVE_FORM, "由原生宿主显示手机登录表单"),
    LoginOption(LoginProvider.QQ, "QQ", LoginInteraction.NATIVE_HANDOFF, "由原生宿主继续 QQ 认证"),
)
DEFAULT_LOGIN_PROVIDER = LoginProvider.WECHAT


@dataclass(frozen=True)
class PhoneLoginSurface:
    """官方电脑版手机登录标签中实机可见的控件。"""

    phone_placeholder: str = "输入手机号"
    code_placeholder: str = "输入验证码"
    request_code_label: str = "获取验证码"
    submit_label: str = "登录"
    consent_notice: str = "登录即同意《用户协议》《隐私政策》"
    requires_consent_checkbox: bool = False


PHONE_LOGIN_SURFACE = PhoneLoginSurface()


class AuthPhase(str, Enum):
    UNAVAILABLE = "unavailable"
    SIGNED_OUT = "signed_out"
    WAITING_FOR_USER = "waiting_for_user"
    QR_PENDING = "qr_pending"
    PHONE_PENDING = "phone_pending"
    PHONE_CODE_PENDING = "phone_code_pending"
    PHONE_SUBMITTING = "phone_submitting"
    QQ_PENDING = "qq_pending"
    VERIFYING = "verifying"
    AUTHENTICATED = "authenticated"
    EXPIRED = "expired"
    ERROR = "error"


@dataclass(frozen=True)
class YuanqiSession:
    """只驻留内存的最小会话对象；repr 不得显示令牌。"""

    uid: str
    device_id: str
    token: str = field(repr=False)
    provider: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.uid and self.device_id and self.token)


@dataclass(frozen=True)
class AuthSnapshot:
    phase: AuthPhase
    message: str
    provider: str = ""
    has_session: bool = False


class YuanqiLoginBackend(Protocol):
    """未来正式宿主需要提供的边界。"""

    def configured(self) -> bool: ...
    def navigate_to_login(self) -> None: ...
    def request_phone_code(self, phone_number: str) -> None: ...
    def submit_phone_login(self, phone_number: str, verification_code: str) -> None: ...
    def get_user_info(self) -> Mapping[str, object]: ...
    def get_device_info(self) -> Mapping[str, object]: ...
    def verify_session(self, session: YuanqiSession) -> bool: ...


class UnavailableYuanqiLoginBackend:
    """没有平台客户端身份时的安全默认实现。"""

    REASON = "缺少元气平台认可的 PC 客户端身份、授权回调和会话校验接口。"

    def configured(self) -> bool:
        return False

    def _unavailable(self):
        raise RuntimeError(self.REASON)

    def navigate_to_login(self) -> None:
        self._unavailable()

    def request_phone_code(self, phone_number: str) -> None:
        self._unavailable()

    def submit_phone_login(self, phone_number: str, verification_code: str) -> None:
        self._unavailable()

    def get_user_info(self) -> Mapping[str, object]:
        return self._unavailable()

    def get_device_info(self) -> Mapping[str, object]:
        return self._unavailable()

    def verify_session(self, session: YuanqiSession) -> bool:
        return self._unavailable()


class YuanqiAuthCoordinator:
    """复刻官方 H5 与原生宿主之间的登录状态流。"""

    def __init__(self, backend: YuanqiLoginBackend):
        self.backend = backend
        self._session: YuanqiSession | None = None
        self._snapshot = AuthSnapshot(
            AuthPhase.SIGNED_OUT if backend.configured() else AuthPhase.UNAVAILABLE,
            "等待用户登录。" if backend.configured() else UnavailableYuanqiLoginBackend.REASON,
        )

    @property
    def snapshot(self) -> AuthSnapshot:
        return self._snapshot

    def begin_login(self) -> AuthSnapshot:
        if not self.backend.configured():
            return self._snapshot
        self.backend.navigate_to_login()
        self._snapshot = AuthSnapshot(
            AuthPhase.QR_PENDING,
            "已拉起原生登录窗，默认等待用户使用微信扫码并在手机端确认。",
            provider=DEFAULT_LOGIN_PROVIDER.value,
        )
        return self._snapshot

    def select_login_provider(self, provider: LoginProvider) -> AuthSnapshot:
        """记录原生登录窗当前入口；认证交互仍必须由用户本人完成。"""

        if not self.backend.configured():
            return self._snapshot
        if provider is LoginProvider.WECHAT:
            self._snapshot = AuthSnapshot(
                AuthPhase.QR_PENDING,
                "等待用户使用微信扫码并在手机端确认。",
                provider=provider.value,
            )
        elif provider is LoginProvider.PHONE:
            self._snapshot = AuthSnapshot(
                AuthPhase.PHONE_PENDING,
                "等待用户在原生手机登录表单中完成认证。",
                provider=provider.value,
            )
        else:
            self._snapshot = AuthSnapshot(
                AuthPhase.QQ_PENDING,
                "等待用户通过原生 QQ 入口完成认证。",
                provider=provider.value,
            )
        return self._snapshot

    def request_phone_code(self, phone_number: str) -> AuthSnapshot:
        """请求短信验证码；手机号只瞬时传给后端，不进入状态对象。"""

        if not self.backend.configured():
            return self._snapshot
        if self._snapshot.provider != LoginProvider.PHONE.value:
            raise RuntimeError("必须先进入手机登录页。")
        if not phone_number.strip():
            raise ValueError("手机号不能为空。")
        self.backend.request_phone_code(phone_number)
        self._snapshot = AuthSnapshot(
            AuthPhase.PHONE_CODE_PENDING,
            "验证码请求已提交，等待用户输入收到的验证码。",
            provider=LoginProvider.PHONE.value,
        )
        return self._snapshot

    def submit_phone_login(self, phone_number: str, verification_code: str) -> AuthSnapshot:
        """提交手机认证；手机号和验证码均不保存在协调器中。"""

        if not self.backend.configured():
            return self._snapshot
        if self._snapshot.provider != LoginProvider.PHONE.value:
            raise RuntimeError("必须先进入手机登录页。")
        if not phone_number.strip() or not verification_code.strip():
            raise ValueError("手机号和验证码不能为空。")
        self.backend.submit_phone_login(phone_number, verification_code)
        self._snapshot = AuthSnapshot(
            AuthPhase.PHONE_SUBMITTING,
            "手机认证已提交，等待原生宿主返回登录回调。",
            provider=LoginProvider.PHONE.value,
        )
        return self._snapshot

    def callback_on_login(self, result: bool | Mapping[str, object]) -> AuthSnapshot:
        """处理 callbackOnLogin；成功后必须重新取用户和设备信息并校验。"""

        success = result if isinstance(result, bool) else bool(
            result.get("success") or result.get("ok") or result.get("ret") == 0
        )
        if not success:
            self._session = None
            self._snapshot = AuthSnapshot(AuthPhase.SIGNED_OUT, "用户取消登录或认证失败。")
            return self._snapshot

        self._snapshot = AuthSnapshot(AuthPhase.VERIFYING, "登录回调已收到，正在校验账号、设备和会话。")
        try:
            user_info = self.backend.get_user_info()
            device_info = self.backend.get_device_info()
            session = YuanqiSession(
                uid=str(user_info.get("uid") or user_info.get("open_id") or ""),
                token=str(user_info.get("token") or ""),
                device_id=str(device_info.get("deviceId") or device_info.get("device_id") or ""),
                provider=str(user_info.get("provider") or user_info.get("login_type") or ""),
            )
            if not session.complete:
                raise ValueError("登录回调缺少完整的用户、令牌或设备信息。")
            if not self.backend.verify_session(session):
                self._session = None
                self._snapshot = AuthSnapshot(AuthPhase.EXPIRED, "服务端拒绝会话，需要重新登录。")
                return self._snapshot
            self._session = session
            self._snapshot = AuthSnapshot(
                AuthPhase.AUTHENTICATED,
                "账号、设备和服务端会话均已验证。",
                provider=session.provider,
                has_session=True,
            )
        except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            self._session = None
            self._snapshot = AuthSnapshot(AuthPhase.ERROR, str(exc))
        return self._snapshot

    def clear(self) -> AuthSnapshot:
        self._session = None
        self._snapshot = AuthSnapshot(
            AuthPhase.SIGNED_OUT if self.backend.configured() else AuthPhase.UNAVAILABLE,
            "已清除本客户端内存会话。" if self.backend.configured() else UnavailableYuanqiLoginBackend.REASON,
        )
        return self._snapshot
