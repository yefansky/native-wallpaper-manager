"""壁纸目录数据源契约。

这里不猜测第三方接口，也不保存外部账号凭证。正式平台适配器必须显式提供
授权、目录与下载能力；缺少官方配置时，界面应显示不可用，而不是返回模拟数据。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class IntegrationUnavailable(RuntimeError):
    """数据源没有正式授权或接口时抛出。"""


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


@dataclass(frozen=True)
class CatalogPage:
    items: tuple[CatalogItem, ...]
    page: int
    has_more: bool


class CatalogSource(Protocol):
    def capabilities(self) -> SourceCapabilities: ...
    def begin_authorization(self) -> str: ...
    def list_categories(self) -> tuple[CatalogCategory, ...]: ...
    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage: ...
    def download_original(self, item_id: str) -> bytes: ...


class YuanqiOfficialApiSource:
    """元气正式接口占位实现。

    只有平台提供可供第三方使用的 OAuth/API 配置后，才能把这个类替换为可用
    实现。当前类故意拒绝返回任何模拟分类、资源或登录态。
    """

    _REASON = "元气公开站尚未提供第三方 OAuth、目录 API 或会员原图下载 API。"

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            source_name="元气桌面正式 API",
            authorization=False,
            categories=False,
            paging=False,
            search=False,
            preview=False,
            original_download=False,
            reason=self._REASON,
        )

    def _unavailable(self):
        raise IntegrationUnavailable(self._REASON)

    def begin_authorization(self) -> str:
        return self._unavailable()

    def list_categories(self) -> tuple[CatalogCategory, ...]:
        return self._unavailable()

    def list_items(self, category_id: str, page: int = 1, query: str = "") -> CatalogPage:
        return self._unavailable()

    def download_original(self, item_id: str) -> bytes:
        return self._unavailable()
