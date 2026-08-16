"""元气静态壁纸资源目录适配器。

登录会话由扫码窗口产生并只保存在内存中。目录、建议词和图片均来自资源服务；
适配器不读取官方客户端数据，也不会在异常、repr 或缓存里持久化 token。
"""
from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol
from urllib.parse import urlparse

import requests
from PIL import Image


CATALOG_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/static/list"
SUGGEST_URL = "https://pcwallpaper.zhhainiao.com/v1/suggestion/suggest"
PAGE_SIZE = 24
FULL_INDEX_PAGE_SIZE = 50
MAX_IMAGE_BYTES = 64 * 1024 * 1024
RESOURCE_IMAGE_HOSTS = {
    "img-baofun.zhhainiao.com",
    "mobile-img-baofun.zhhainiao.com",
    "cdnpcwallpaper.zhhainiao.com",
    "wallpaper-static.cheetahfun.com",
}
_RESOURCE_IMAGE_HOST_SUFFIXES = (".baofun.zhhainiao.com", ".cheetahfun.com")


class IntegrationUnavailable(RuntimeError):
    """没有完整登录会话或服务端拒绝请求。"""


class CatalogError(RuntimeError):
    """资源服务返回异常数据。"""


@dataclass(frozen=True)
class SourceCapabilities:
    source_name: str
    authorization: bool
    categories: bool
    paging: bool
    search: bool
    preview: bool
    original_download: bool
    reason: str = ""


@dataclass(frozen=True)
class CatalogCategory:
    id: str
    title: str


@dataclass(frozen=True)
class CatalogItem:
    id: str
    title: str
    preview_url: str
    original_url: str | None = None
    category_id: str = ""
    category_name: str = ""
    author: str = ""
    tags: tuple[str, ...] = ()
    resolution: str = ""
    format: str = ""
    size_bytes: int = 0
    expected_md5: str = ""
    likes: int = 0
    views: int = 0
    price: int = 0

    @property
    def search_text(self) -> str:
        return " ".join((self.title, self.category_name, self.author, *self.tags)).casefold()


@dataclass(frozen=True)
class CatalogPage:
    items: tuple[CatalogItem, ...]
    page: int
    has_more: bool
    total: int = 0


class CatalogSource(Protocol):
    def capabilities(self) -> SourceCapabilities: ...
    def begin_authorization(self) -> str: ...
    def list_categories(self) -> tuple[CatalogCategory, ...]: ...
    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage: ...
    def download_original(self, item_id: str) -> bytes: ...


def validate_resource_image_url(url: str) -> str:
    """只允许已知元气图片 CDN 上的 HTTPS 静态图片地址。

    服务器给出的路径可能不带扩展名（例如 `/pcwallpaper_ugc/static/<md5>`），
    因此安全边界以主机白名单为准；图片真实性由下载时的 Content-Type 与 PIL
    解码校验。
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    allowed = host in RESOURCE_IMAGE_HOSTS or host.endswith(_RESOURCE_IMAGE_HOST_SUFFIXES)
    if parsed.scheme != "https" or not allowed:
        raise CatalogError("服务端返回了不受信任的图片地址")
    if not parsed.path or parsed.path == "/":
        raise CatalogError("服务端返回的图片地址缺少路径")
    return url


class YuanqiResourceSource:
    """使用自研扫码会话访问元气 PC 静态壁纸资源。"""

    _CATEGORIES = (
        CatalogCategory("0", "精选"),
        CatalogCategory("__4k__", "4K"),
        CatalogCategory("1", "风景"),
        CatalogCategory("8", "小清新"),
        CatalogCategory("2", "动漫"),
        CatalogCategory("6", "明星"),
        CatalogCategory("5", "美女"),
        CatalogCategory("9", "科幻"),
        CatalogCategory("7", "动物"),
        CatalogCategory("4", "游戏"),
        CatalogCategory("12", "体育"),
        CatalogCategory("10", "汽车"),
        CatalogCategory("11", "军事"),
        CatalogCategory("13", "其他"),
    )

    def __init__(
        self,
        session_provider: Callable[[], Mapping[str, object]] | None = None,
        device_provider: Callable[[], Mapping[str, object]] | None = None,
        *,
        http: object = requests,
    ):
        self._session_provider = session_provider
        self._device_provider = device_provider
        self._http = http
        self._items_by_id: dict[str, CatalogItem] = {}
        self._full_index: tuple[CatalogItem, ...] | None = None
        self._index_lock = threading.Lock()

    def capabilities(self) -> SourceCapabilities:
        configured = self._session_provider is not None and self._device_provider is not None
        return SourceCapabilities(
            source_name="元气桌面联网静态资源",
            authorization=configured,
            categories=True,
            paging=True,
            search=True,
            preview=True,
            original_download=True,
            reason="" if configured else "请先用本客户端完成微信扫码登录",
        )

    def begin_authorization(self) -> str:
        return "wechat_qr"

    def list_categories(self) -> tuple[CatalogCategory, ...]:
        return self._CATEGORIES

    def _common(self) -> dict[str, object]:
        if self._session_provider is None or self._device_provider is None:
            raise IntegrationUnavailable("请先完成微信扫码登录")
        user = self._session_provider()
        device = self._device_provider()
        token = str(user.get("token") or "")
        open_id = str(user.get("uid") or user.get("open_id") or "")
        device_id = str(device.get("deviceId") or device.get("device_id") or "")
        if not (token and open_id and device_id):
            raise IntegrationUnavailable("登录会话不完整，请重新扫码")
        return {
            "platform": "pc",
            "open_id": open_id,
            "token": token,
            "device_id": device_id,
            "player_version": 1,
        }

    def _post(self, url: str, body: dict[str, object]) -> Mapping[str, object]:
        try:
            response = self._http.post(url, json=body, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except (OSError, ValueError, requests.RequestException) as exc:
            raise CatalogError(f"联网资源请求失败：{type(exc).__name__}") from exc
        if not isinstance(payload, Mapping):
            raise CatalogError("资源服务返回格式错误")
        common = payload.get("resp_common") or {}
        ret = common.get("ret") if isinstance(common, Mapping) else None
        if ret != 0:
            raise IntegrationUnavailable(f"资源服务拒绝请求（ret={ret}），请重新登录或检查账号权限")
        return payload

    @staticmethod
    def _parse_item(raw: Mapping[str, object]) -> CatalogItem:
        tags_raw = raw.get("tags") or []
        tags = tuple(
            str(tag.get("tname") or "")
            for tag in tags_raw
            if isinstance(tag, Mapping) and tag.get("tname")
        )
        original = str(raw.get("jpg_url") or raw.get("jpg_1920_url") or "")
        preview = str(raw.get("small_jpg_url") or raw.get("mid_jpg_url") or raw.get("jpg_1920_url") or original)
        if preview:
            validate_resource_image_url(preview)
        if original:
            validate_resource_image_url(original)
        return CatalogItem(
            id=str(raw.get("id") or raw.get("wallpaper_id") or ""),
            title=str(raw.get("wname") or raw.get("title") or "未命名壁纸"),
            preview_url=preview,
            original_url=original or None,
            category_id=str(raw.get("cid") or ""),
            category_name=str(raw.get("cname") or ""),
            author=str(raw.get("author") or ""),
            tags=tags,
            resolution=str(raw.get("resolution") or ""),
            format=str(raw.get("format") or ""),
            size_bytes=int(raw.get("size") or raw.get("storage") or 0),
            expected_md5=str(raw.get("md5") or "").lower(),
            likes=int(raw.get("likes") or 0),
            views=int(raw.get("view") or 0),
            price=int(raw.get("price") or 0),
        )

    def _list_server_page(self, category_id: str, page: int, page_size: int = PAGE_SIZE) -> CatalogPage:
        if page < 1:
            raise ValueError("页码必须从 1 开始")
        body: dict[str, object] = {
            "login_info": {},
            "common": self._common(),
            "page": page,
            "page_size": page_size,
            "cate_id": int(category_id or "0"),
            "tag_id": 0,
            "sort_type": 1,
        }
        payload = self._post(CATALOG_URL, body)
        data = payload.get("data") or {}
        if not isinstance(data, Mapping):
            raise CatalogError("资源列表结构错误")
        raw_items = data.get("list") or []
        if not isinstance(raw_items, list):
            raise CatalogError("资源列表结构错误")
        items = tuple(self._parse_item(raw) for raw in raw_items if isinstance(raw, Mapping))
        self._items_by_id.update({item.id: item for item in items if item.id})
        # 部分类别的 total 是全站总数，不能用它判断末页；以实际页大小为准。
        has_more = len(raw_items) >= PAGE_SIZE
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return CatalogPage(items, page, has_more, total)

    def _ensure_full_index(
        self, progress_cb: Callable[[int, int], None] | None = None
    ) -> tuple[CatalogItem, ...]:
        if self._full_index is not None:
            return self._full_index
        with self._index_lock:
            if self._full_index is not None:
                return self._full_index
            collected: dict[str, CatalogItem] = {}
            page = 1
            while page <= 100:
                if progress_cb:
                    progress_cb(len(collected), page)
                result = self._list_server_page("0", page, FULL_INDEX_PAGE_SIZE)
                for item in result.items:
                    collected[item.id] = item
                if not result.has_more:
                    break
                # 全站 total 在此接口可靠，可避免末尾额外请求。
                if result.total and len(collected) >= result.total:
                    break
                page += 1
            self._full_index = tuple(collected.values())
            return self._full_index

    def search(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = PAGE_SIZE,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> CatalogPage:
        """搜索壁纸：基于后台同步的全站索引做本地过滤，索引缓存后无需重复同步。"""
        keyword = keyword.strip()
        if not keyword:
            return CatalogPage((), page, False, 0)
        index = self._ensure_full_index(progress_cb)
        terms = tuple(part for part in keyword.casefold().split() if part)
        filtered = [item for item in index if all(term in item.search_text for term in terms)]
        start = (page - 1) * page_size
        items = tuple(filtered[start:start + page_size])
        return CatalogPage(items, page, start + len(items) < len(filtered), len(filtered))

    @staticmethod
    def _is_4k(item: CatalogItem) -> bool:
        text = f"{item.resolution} {' '.join(item.tags)}".casefold()
        if "4k" in text:
            return True
        numbers = [int(part) for part in text.replace("×", "x").split("x") if part.strip().isdigit()]
        return bool(numbers and max(numbers) >= 3840)

    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage:
        query = query.strip().casefold()
        if not query and category_id != "__4k__":
            return self._list_server_page(category_id or "0", page)
        index = self._ensure_full_index()
        filtered = [item for item in index if category_id in ("", "0", "__4k__") or item.category_id == category_id]
        if category_id == "__4k__":
            filtered = [item for item in filtered if self._is_4k(item)]
        if query:
            terms = tuple(part for part in query.split() if part)
            filtered = [item for item in filtered if all(term in item.search_text for term in terms)]
        start = (page - 1) * PAGE_SIZE
        items = tuple(filtered[start:start + PAGE_SIZE])
        return CatalogPage(items, page, start + len(items) < len(filtered), len(filtered))

    def suggestions(self, query: str) -> tuple[str, ...]:
        query = query.strip()
        if not query:
            return ()
        body = {"common": self._common(), "search_key": query}
        payload = self._post(SUGGEST_URL, body)
        results = payload.get("results") or []
        return tuple(str(value).strip() for value in results if str(value).strip())[:10]

    def download_original(self, item_id: str) -> bytes:
        item = self._items_by_id.get(str(item_id))
        if item is None or not item.original_url:
            raise CatalogError("当前资源没有可下载的静态原图")
        url = validate_resource_image_url(item.original_url)
        try:
            response = self._http.get(url, timeout=45, stream=True)
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                raise CatalogError("下载响应不是图片")
            output = io.BytesIO()
            digest = hashlib.md5()
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                if output.tell() + len(chunk) > MAX_IMAGE_BYTES:
                    raise CatalogError("原图超过 64 MiB 安全上限")
                output.write(chunk)
                digest.update(chunk)
            data = output.getvalue()
            if not data:
                raise CatalogError("下载得到空文件")
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        except CatalogError:
            raise
        except (OSError, ValueError, requests.RequestException) as exc:
            raise CatalogError(f"原图下载失败：{type(exc).__name__}") from exc
        if item.expected_md5 and digest.hexdigest().lower() != item.expected_md5:
            raise CatalogError("原图 MD5 与服务端元数据不一致")
        return data


class YuanqiOfficialApiSource:
    """兼容旧调用点：没有会话提供器时明确不可用。"""

    _REASON = "请先用本客户端完成微信扫码登录。"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities("元气桌面联网静态资源", False, True, True, True, True, False, self._REASON)

    def _unavailable(self):
        raise IntegrationUnavailable(self._REASON)

    def begin_authorization(self) -> str:
        return "wechat_qr"

    def list_categories(self) -> tuple[CatalogCategory, ...]:
        return YuanqiResourceSource._CATEGORIES

    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage:
        return self._unavailable()

    def download_original(self, item_id: str) -> bytes:
        return self._unavailable()
