"""元气静态壁纸资源目录适配器。

登录会话由扫码窗口产生并只保存在内存中。目录、建议词和图片均来自资源服务；
适配器不读取官方客户端数据，也不会在异常、repr 或缓存里持久化 token。
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol
from urllib.parse import urlparse

import requests
from PIL import Image

from dynamic_converter import ConvertedFrame, ConversionUnavailable, convert_preview_video_to_jpeg


CATALOG_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/static/list"
INDEX_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/static/index"
SEARCH_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/static/search"
CATEGORY_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/static/cate_nav_v2"
FOLLOWING_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/static/following/list"
SUGGEST_URL = "https://pcwallpaper.zhhainiao.com/v1/suggestion/suggest"
DYNAMIC_LIST_URL = "https://pcwallpaper.zhhainiao.com/v20903/wplive/list"
DYNAMIC_INDEX_URL = "https://pcwallpaper.zhhainiao.com/v20526/wplive/index"
DYNAMIC_SEARCH_URL = "https://pcwallpaper.zhhainiao.com/v20526/wplive/opensearch/with/ptype"
DYNAMIC_CATEGORY_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/live/cate_nav_v2"
DYNAMIC_FOLLOWING_URL = "https://pcwallpaper.zhhainiao.com/v1/wallpaper/live/following/list"
DYNAMIC_NEWEST_URL = "https://pcwallpaper.zhhainiao.com/wplive/list/newest"
THEME_CATEGORY_URL = "https://cdnpcwallpaper.zhhainiao.com/config/get/by/ckey?ckey=theme_category"
THEME_FEATURED_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/live/themes_by_page"
THEME_CAROUSEL_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/live/themes"
THEME_LIST_URL = "https://pcwallpaper.zhhainiao.com/wallpaper/live/theme/list"
PAGE_SIZE = 24
SORT_HOT_DESC = 2
SORT_TIME_DESC = 1
MAX_IMAGE_BYTES = 64 * 1024 * 1024
RESOURCE_IMAGE_HOSTS = {
    "img-baofun.zhhainiao.com",
    "mobile-img-baofun.zhhainiao.com",
    "cdnpcwallpaper.zhhainiao.com",
    "wallpaper-static.cheetahfun.com",
}
_RESOURCE_IMAGE_HOST_SUFFIXES = (".baofun.zhhainiao.com", ".cheetahfun.com")
_RESOURCE_VIDEO_HOST_SUFFIXES = _RESOURCE_IMAGE_HOST_SUFFIXES


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
    resource_kind: str = "static"
    preview_video_url: str = ""
    source_video_url: str = ""
    components: tuple[str, ...] = ()

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


def validate_resource_preview_video_url(url: str) -> str:
    """只接受元气图片 CDN 上公开、标准的 preview MP4。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    allowed = host in RESOURCE_IMAGE_HOSTS or host.endswith(_RESOURCE_VIDEO_HOST_SUFFIXES)
    if parsed.scheme != "https" or not allowed:
        raise CatalogError("服务端返回了不受信任的动态预览地址")
    if not parsed.path.lower().endswith("_preview.mp4"):
        raise CatalogError("动态资源没有标准 preview_video，拒绝读取完整视频或私有容器")
    return url


def validate_resource_source_video_url(url: str) -> str:
    """允许元气可信 CDN 上的正式视频或私有容器；内容仍须经解码器验证。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    allowed = host in RESOURCE_IMAGE_HOSTS or host.endswith(_RESOURCE_VIDEO_HOST_SUFFIXES)
    if parsed.scheme != "https" or not allowed:
        raise CatalogError("服务端返回了不受信任的正式视频地址")
    if not parsed.path or parsed.path == "/":
        raise CatalogError("服务端返回的正式视频地址缺少路径")
    return url


class YuanqiResourceSource:
    """使用自研扫码会话访问元气 PC 静态壁纸资源。"""

    _SPECIAL_CATEGORIES = (
        CatalogCategory("__following__", "关注"),
        CatalogCategory("__featured__", "精选"),
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
        self._server_categories: tuple[CatalogCategory, ...] | None = None
        self._category_lock = threading.Lock()

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
        return self._SPECIAL_CATEGORIES + (self._server_categories or ())

    def refresh_categories(self) -> tuple[CatalogCategory, ...]:
        """读取元气静态页的服务端导航，保留其顺序与 cid/tid 语义。"""
        if self._server_categories is not None:
            return self.list_categories()
        with self._category_lock:
            if self._server_categories is not None:
                return self.list_categories()
            payload = self._post(CATEGORY_URL, {"login_info": {}, "common": self._common()})
            raw_categories = payload.get("data") or []
            if not isinstance(raw_categories, list):
                raise CatalogError("分类导航结构错误")
            categories: list[CatalogCategory] = []
            seen: set[str] = set()
            for raw in raw_categories:
                if not isinstance(raw, Mapping):
                    continue
                if raw.get("tid") is not None:
                    category_id = f"tag:{int(raw['tid'])}"
                    title = str(raw.get("tname") or "").strip()
                elif raw.get("cid") is not None:
                    category_id = f"cate:{int(raw['cid'])}"
                    title = str(raw.get("cname") or "").strip()
                else:
                    continue
                if title and category_id not in seen:
                    seen.add(category_id)
                    categories.append(CatalogCategory(category_id, title))
            self._server_categories = tuple(categories)
            return self.list_categories()

    def reset_session_cache(self) -> None:
        """登录态变化后丢弃账号相关目录缓存，不持久化任何会话数据。"""
        with self._category_lock:
            self._server_categories = None
        self._items_by_id.clear()

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

    @staticmethod
    def _selector(category_id: str) -> tuple[int, int]:
        try:
            if category_id.startswith("tag:"):
                return 0, int(category_id.split(":", 1)[1])
            if category_id.startswith("cate:"):
                return int(category_id.split(":", 1)[1]), 0
            # 兼容旧缓存键和调用方；纯数字在官方协议中表示 cid。
            return int(category_id or "0"), 0
        except (TypeError, ValueError) as exc:
            raise CatalogError("分类标识不是资源服务返回的 cid/tid") from exc

    def _page_from_list_payload(self, payload: Mapping[str, object], page: int) -> CatalogPage:
        data = payload.get("data") or {}
        if not isinstance(data, Mapping):
            raise CatalogError("资源列表结构错误")
        raw_items = data.get("list") or []
        if not isinstance(raw_items, list):
            raise CatalogError("资源列表结构错误")
        if not raw_items and isinstance(data.get("recommend_list"), list):
            raw_items = data["recommend_list"]
        items = tuple(self._parse_item(raw) for raw in raw_items if isinstance(raw, Mapping))
        self._items_by_id.update({item.id: item for item in items if item.id})
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return CatalogPage(items, page, len(raw_items) >= PAGE_SIZE, total)

    def _list_server_page(
        self,
        category_id: str,
        page: int,
        page_size: int = PAGE_SIZE,
        *,
        sort_type: int = SORT_HOT_DESC,
        following: bool = False,
    ) -> CatalogPage:
        if page < 1:
            raise ValueError("页码必须从 1 开始")
        cate_id, tag_id = self._selector(category_id)
        body: dict[str, object] = {
            "login_info": {},
            "common": self._common(),
            "page": page,
            "page_size": page_size,
            "cate_id": cate_id,
            "tag_id": tag_id,
            "sort_type": sort_type,
        }
        return self._page_from_list_payload(self._post(FOLLOWING_URL if following else CATALOG_URL, body), page)

    def _featured_page(self, page: int, page_size: int = PAGE_SIZE) -> CatalogPage:
        if page < 1:
            raise ValueError("页码必须从 1 开始")
        body = {
            "login_info": {},
            "common": self._common(),
            "count": page_size,
            "offset": (page - 1) * page_size,
        }
        payload = self._post(INDEX_URL, body)
        raw_items = payload.get("data") or []
        if not isinstance(raw_items, list):
            raise CatalogError("精选资源结构错误")
        items = tuple(self._parse_item(raw) for raw in raw_items if isinstance(raw, Mapping))
        self._items_by_id.update({item.id: item for item in items if item.id})
        try:
            total = int(payload.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return CatalogPage(items, page, len(raw_items) >= page_size, total)

    def search(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = PAGE_SIZE,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> CatalogPage:
        """调用元气静态页使用的全局搜索接口，不继承当前分类。"""
        keyword = keyword.strip()
        if not keyword:
            return CatalogPage((), page, False, 0)
        if page < 1:
            raise ValueError("页码必须从 1 开始")
        body = {
            "login_info": {},
            "common": self._common(),
            "kw": keyword,
            "page": page,
            "page_size": page_size,
        }
        return self._page_from_list_payload(self._post(SEARCH_URL, body), page)

    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage:
        query = query.strip()
        if query:
            return self.search(query, page)
        if category_id == "__following__":
            return self._list_server_page("-1", page, sort_type=SORT_TIME_DESC, following=True)
        if category_id in {"", "0", "__featured__"}:
            return self._featured_page(page)
        return self._list_server_page(category_id, page, sort_type=SORT_HOT_DESC)

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


class YuanqiDynamicResourceSource(YuanqiResourceSource):
    """元气动态目录适配器；优先正式画面，失败时回退标准预览视频。"""

    _SPECIAL_CATEGORIES = (
        CatalogCategory("__following__", "关注"),
        CatalogCategory("__featured__", "精选"),
        CatalogCategory("__newest__", "最新"),
    )

    def __init__(self, *args, converter=convert_preview_video_to_jpeg, **kwargs):
        super().__init__(*args, **kwargs)
        self._converter = converter
        self._converted_by_id: dict[str, ConvertedFrame] = {}
        self._conversion_path_by_id: dict[str, str] = {}

    def reset_session_cache(self) -> None:
        super().reset_session_cache()
        self._converted_by_id.clear()
        self._conversion_path_by_id.clear()

    def capabilities(self) -> SourceCapabilities:
        configured = self._session_provider is not None and self._device_provider is not None
        return SourceCapabilities(
            source_name="元气动态预览转 Windows 静态壁纸",
            authorization=configured,
            categories=True,
            paging=True,
            search=True,
            preview=True,
            original_download=False,
            reason="" if configured else "请先用本客户端完成微信扫码登录",
        )

    def refresh_categories(self) -> tuple[CatalogCategory, ...]:
        """读取当前动态页导航，不在客户端硬编码官方分类。"""
        if self._server_categories is not None:
            return self.list_categories()
        with self._category_lock:
            if self._server_categories is not None:
                return self.list_categories()
            payload = self._post(DYNAMIC_CATEGORY_URL, {"common": self._common()})
            raw_categories = payload.get("data") or []
            if not isinstance(raw_categories, list):
                raise CatalogError("动态分类导航结构错误")
            categories: list[CatalogCategory] = []
            seen: set[str] = set()
            for raw in raw_categories:
                if not isinstance(raw, Mapping):
                    continue
                if raw.get("tid") is not None:
                    category_id = f"tag:{int(raw['tid'])}"
                elif raw.get("cid") is not None:
                    category_id = f"cate:{int(raw['cid'])}"
                else:
                    continue
                title = str(raw.get("tname") or raw.get("cname") or "").strip()
                if title and category_id not in seen:
                    seen.add(category_id)
                    categories.append(CatalogCategory(category_id, title))
            self._server_categories = tuple(categories)
            return self.list_categories()

    @staticmethod
    def _parse_item(raw: Mapping[str, object]) -> CatalogItem:
        tags_raw = raw.get("tags") or raw.get("tag") or []
        tags = tuple(
            str(tag.get("tname") or "")
            for tag in tags_raw
            if isinstance(tag, Mapping) and tag.get("tname")
        )
        components: list[str] = []
        try:
            resource_detail = json.loads(str(raw.get("resource_detail") or "{}"))
            composites = resource_detail.get("composites") or []
            for composite in composites:
                if not isinstance(composite, Mapping):
                    continue
                value = str(composite.get("sub_type") or composite.get("type") or "").strip()
                if value and value not in components:
                    components.append(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            is_theme = int(raw.get("sub_wtype") or 0) == 2 or int(raw.get("theme_type") or 0) == 1
        except (TypeError, ValueError):
            is_theme = False
        preview = str(raw.get("preview_jpg_raw") or raw.get("preview_jpg_mid") or raw.get("preview_jpg") or "")
        preview_video = str(raw.get("preview_video") or "")
        # 干净高清源视频：优先 4K/2K/1920，最后回退基础 video（均带 XOR 混淆）。
        source_video = (
            str(raw.get("video_4k") or raw.get("video_2k") or raw.get("video_1920") or raw.get("video") or "")
        )
        if preview:
            validate_resource_image_url(preview)
        if preview_video:
            validate_resource_preview_video_url(preview_video)
        return CatalogItem(
            id=str(raw.get("wid") or raw.get("id") or ""),
            title=str(raw.get("wname") or raw.get("title") or "未命名动态壁纸"),
            preview_url=preview,
            original_url=preview or None,
            category_id=str(raw.get("cate_id") or raw.get("cid") or ""),
            category_name=str(raw.get("cname") or "动态转静态"),
            author=str(raw.get("author_name") or raw.get("author") or ""),
            tags=tags,
            resolution=str(raw.get("resolution") or ""),
            format=str(raw.get("format") or "动态预览 MP4"),
            size_bytes=int(raw.get("size") or raw.get("storage") or 0),
            likes=int(raw.get("likes") or 0),
            views=int(raw.get("download_count") or raw.get("view") or 0),
            price=int(raw.get("price") or raw.get("ptype") or 0),
            resource_kind="theme" if is_theme else "dynamic",
            preview_video_url=preview_video,
            source_video_url=source_video,
            components=tuple(components),
        )

    def _dynamic_body(self, page: int, page_size: int) -> dict[str, object]:
        if page < 1:
            raise ValueError("页码必须从 1 开始")
        return {
            "common": self._common(),
            "resolution_support": 0,
            "wtype_support": 1,
            "encrypt_support": "none_encrypt",
            "page": page,
            "page_size": page_size,
        }

    def _featured_page(self, page: int, page_size: int = PAGE_SIZE) -> CatalogPage:
        body = self._dynamic_body(page, page_size)
        body.pop("page")
        body.pop("page_size")
        body.update({"is_new_user": 0, "offset": (page - 1) * page_size, "count": page_size})
        payload = self._post(DYNAMIC_INDEX_URL, body)
        data = payload.get("data") or {}
        if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
            raise CatalogError("动态精选资源结构错误")
        raw_items = data["items"]
        items = tuple(self._parse_item(raw) for raw in raw_items if isinstance(raw, Mapping))
        self._items_by_id.update({item.id: item for item in items if item.id})
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return CatalogPage(items, page, len(raw_items) >= page_size, total)

    def _special_page(self, url: str, page: int, page_size: int = PAGE_SIZE) -> CatalogPage:
        return self._page_from_list_payload(self._post(url, self._dynamic_body(page, page_size)), page)

    def search(self, keyword: str, page: int = 1, page_size: int = PAGE_SIZE, progress_cb=None) -> CatalogPage:
        keyword = keyword.strip()
        if not keyword:
            return CatalogPage((), page, False, 0)
        body = self._dynamic_body(page, page_size)
        body["kw"] = keyword
        return self._page_from_list_payload(self._post(DYNAMIC_SEARCH_URL, body), page)

    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage:
        query = query.strip()
        if query:
            return self.search(query, page)
        if category_id == "__following__":
            # 当前关注接口一次返回完整集合且忽略 page；禁止把相同作品伪装成下一页。
            if page > 1:
                return CatalogPage((), page, False, 0)
            result = self._special_page(DYNAMIC_FOLLOWING_URL, page)
            return CatalogPage(result.items, page, False, len(result.items))
        if category_id == "__newest__":
            return self._special_page(DYNAMIC_NEWEST_URL, page)
        if category_id in {"", "0", "__featured__"}:
            return self._featured_page(page)
        cate_id, tag_id = self._selector(category_id)
        body = self._dynamic_body(page, PAGE_SIZE)
        body.update({"cate_id": cate_id, "tag_id": tag_id, "sort_type": SORT_HOT_DESC})
        return self._page_from_list_payload(self._post(DYNAMIC_LIST_URL, body), page)

    def download_original(self, item_id: str) -> bytes:
        item = self._items_by_id.get(str(item_id))
        if item is None:
            raise CatalogError("该资源不在当前结果中")
        if not item.source_video_url and not item.preview_video_url:
            raise CatalogError("该资源没有可用视频，无法转换")

        converted: ConvertedFrame | None = None
        source_error: ConversionUnavailable | CatalogError | None = None
        if item.source_video_url:
            try:
                source_url = validate_resource_source_video_url(item.source_video_url)
                converted = self._converter(source_url, http=self._http, decrypt=True)
                self._conversion_path_by_id[str(item_id)] = "高清源"
            except (ConversionUnavailable, CatalogError) as exc:
                # 正式资源可能是真 CWP3/主题包，不能验证时安全回退标准预览视频。
                source_error = exc

        if converted is None and item.preview_video_url:
            try:
                preview_url = validate_resource_preview_video_url(item.preview_video_url)
                converted = self._converter(preview_url, http=self._http, decrypt=False)
                self._conversion_path_by_id[str(item_id)] = "预览回退" if source_error else "预览视频"
            except ConversionUnavailable as exc:
                if source_error is not None:
                    raise CatalogError(f"正式资源无法解析，预览回退也失败：{exc}") from exc
                raise CatalogError(str(exc)) from exc

        if converted is None:
            assert source_error is not None
            raise CatalogError(f"正式资源无法解析且没有预览回退：{source_error}") from source_error
        self._converted_by_id[str(item_id)] = converted
        return converted.jpeg

    def conversion_summary(self, item_id: str) -> str:
        converted = self._converted_by_id.get(str(item_id))
        if converted is None:
            return ""
        cache_note = " · 缓存命中" if converted.from_cache else ""
        conversion_path = self._conversion_path_by_id.get(str(item_id), "视频")
        return (
            f"{conversion_path} · {converted.width}×{converted.height} · "
            f"{converted.candidate_count} 点择优 · {converted.timestamp:g} 秒{cache_note}"
        )


class YuanqiThemeResourceSource(YuanqiDynamicResourceSource):
    """元气电脑主题目录；正式画面不可验证时降级使用标准 preview_video。"""

    _SPECIAL_CATEGORIES = (CatalogCategory("__featured__", "精选"),)
    _FEATURED_PAGE_SIZE = 12

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._theme_filters: dict[str, dict[str, object]] = {}
        self._featured_pages: dict[int, CatalogPage] = {}
        self._featured_cursors: dict[int, str] = {1: ""}
        self._carousel_items: tuple[CatalogItem, ...] | None = None

    def capabilities(self) -> SourceCapabilities:
        configured = self._session_provider is not None and self._device_provider is not None
        return SourceCapabilities(
            source_name="元气电脑主题转 Windows 原生静态主题",
            authorization=configured,
            categories=True,
            paging=True,
            search=True,
            preview=True,
            original_download=False,
            reason="" if configured else "请先用本客户端完成微信扫码登录",
        )

    def reset_session_cache(self) -> None:
        super().reset_session_cache()
        self._theme_filters.clear()
        self._featured_pages.clear()
        self._featured_cursors = {1: ""}
        self._carousel_items = None

    def refresh_categories(self) -> tuple[CatalogCategory, ...]:
        if self._server_categories is not None:
            return self.list_categories()
        with self._category_lock:
            if self._server_categories is not None:
                return self.list_categories()
            try:
                response = self._http.get(THEME_CATEGORY_URL, timeout=20)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise CatalogError("电脑主题导航结构错误")
                common = payload.get("resp_common") or {}
                if not isinstance(common, Mapping) or common.get("ret") != 0:
                    raise CatalogError("电脑主题导航服务拒绝请求")
                raw_categories = json.loads(str(payload.get("data") or "[]"))
            except CatalogError:
                raise
            except (OSError, TypeError, ValueError, requests.RequestException, json.JSONDecodeError) as exc:
                raise CatalogError(f"电脑主题导航加载失败：{type(exc).__name__}") from exc
            if not isinstance(raw_categories, list):
                raise CatalogError("电脑主题导航结构错误")
            categories: list[CatalogCategory] = []
            filters: dict[str, dict[str, object]] = {}
            for raw in raw_categories:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    server_id = int(raw["cid"])
                    cate_ids = [int(value) for value in (raw.get("cate_ids") or [])]
                    tag_ids = [int(value) for value in (raw.get("tag_ids") or [])]
                    min_mark = int(raw.get("min_mark") or 0)
                    sort_type = int(raw.get("sort_type") or SORT_TIME_DESC)
                except (KeyError, TypeError, ValueError):
                    continue
                title = str(raw.get("cname") or "").strip()
                if not title:
                    continue
                category_id = f"theme:{server_id}"
                categories.append(CatalogCategory(category_id, title))
                filters[category_id] = {
                    "cate_ids": cate_ids,
                    "tag_ids": tag_ids,
                    "min_mark": min_mark,
                    "sort_type": sort_type,
                }
            self._theme_filters = filters
            self._server_categories = tuple(categories)
            return self.list_categories()

    def _theme_items(self, raw_items: object) -> tuple[CatalogItem, ...]:
        if not isinstance(raw_items, list):
            raise CatalogError("电脑主题资源结构错误")
        items = tuple(self._parse_item(raw) for raw in raw_items if isinstance(raw, Mapping))
        items = tuple(item for item in items if item.resource_kind == "theme")
        self._items_by_id.update({item.id: item for item in items if item.id})
        return items

    def _carousel(self) -> tuple[CatalogItem, ...]:
        if self._carousel_items is None:
            payload = self._post(
                THEME_CAROUSEL_URL,
                {"login_info": {}, "common": self._common()},
            )
            self._carousel_items = self._theme_items(payload.get("data") or ())[:3]
        return self._carousel_items

    def _featured_page(self, page: int) -> CatalogPage:
        if page < 1:
            raise ValueError("页码必须从 1 开始")
        for current in range(1, page + 1):
            if current in self._featured_pages:
                continue
            if current not in self._featured_cursors:
                return CatalogPage((), page, False, 0)
            payload = self._post(
                THEME_FEATURED_URL,
                {
                    "login_info": {},
                    "common": self._common(),
                    "size": self._FEATURED_PAGE_SIZE,
                    "cursor": self._featured_cursors[current],
                    "is_new_user": 0,
                },
            )
            items = self._theme_items(payload.get("data") or ())
            if current == 1:
                seen = {item.id for item in items}
                items = tuple(item for item in self._carousel() if item.id not in seen) + items
            has_more = bool(payload.get("has_more"))
            next_cursor = str(payload.get("cursor") or "")
            self._featured_pages[current] = CatalogPage(items, current, has_more, 0)
            if has_more and next_cursor:
                self._featured_cursors[current + 1] = next_cursor
        return self._featured_pages.get(page, CatalogPage((), page, False, 0))

    def _category_page(self, category_id: str, page: int) -> CatalogPage:
        if page < 1:
            raise ValueError("页码必须从 1 开始")
        if self._server_categories is None:
            self.refresh_categories()
        filters = self._theme_filters.get(category_id)
        if filters is None:
            raise CatalogError("电脑主题分类不是服务端当前导航中的项目")
        body = {
            "common": self._common(),
            **filters,
            "page": page,
            "page_size": PAGE_SIZE,
        }
        payload = self._post(THEME_LIST_URL, body)
        data = payload.get("data") or {}
        if not isinstance(data, Mapping):
            raise CatalogError("电脑主题分类资源结构错误")
        items = self._theme_items(data.get("list") or ())
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return CatalogPage(items, page, page * PAGE_SIZE < total, total)

    def search(self, keyword: str, page: int = 1, page_size: int = PAGE_SIZE, progress_cb=None) -> CatalogPage:
        # 元气主题页的顶部搜索会进入全局动态搜索；这里只保留其中的主题条目。
        result = super().search(keyword, page, page_size, progress_cb)
        items = tuple(item for item in result.items if item.resource_kind == "theme")
        self._items_by_id.update({item.id: item for item in items if item.id})
        return CatalogPage(items, page, result.has_more, len(items))

    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage:
        if query.strip():
            return self.search(query, page)
        if category_id in {"", "0", "__featured__"}:
            return self._featured_page(page)
        return self._category_page(category_id, page)


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
        return YuanqiResourceSource._SPECIAL_CATEGORIES

    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage:
        return self._unavailable()

    def download_original(self, item_id: str) -> bytes:
        return self._unavailable()
