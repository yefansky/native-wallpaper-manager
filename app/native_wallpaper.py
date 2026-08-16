"""原生壁纸管理器：图库式浏览，且只使用 Windows 原生壁纸接口。"""
from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
import winreg
from concurrent.futures import Future, ThreadPoolExecutor

import requests
from dataclasses import asdict, dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageDraw, ImageOps, ImageTk
import pystray

from catalog import (
    CatalogError,
    CatalogItem,
    CatalogPage,
    IntegrationUnavailable,
    YuanqiDynamicResourceSource,
    YuanqiResourceSource,
)
from product import SingleInstance, autostart_enabled, set_autostart
from yuanqi_auth import (
    AuthPhase,
    BRIDGE_METHODS,
    LOGIN_OPTIONS,
    PHONE_LOGIN_SURFACE,
    QQ_LOGIN_SURFACE,
    QQ_AUTH_RETURN_SURFACE,
    KVIPGUI_LOGIN_BRIDGE_BOUNDARY,
    WECHAT_LOGIN_SURFACE,
    WECHAT_AUTH_RETURN_SURFACE,
    YuanqiAuthCoordinator,
)
from session_store import clear_session, load_session, save_session
from wechat_login import WechatQrLoginBackend, log_diag, sha8

APP_DIR = Path.home() / "AppData" / "Local" / "NativeWallpaperManager"
WALLPAPER_DIR = APP_DIR / "wallpapers"
CONFIG_PATH = APP_DIR / "config.json"
THEME_DIR = APP_DIR / "themes"
IMAGE_EXTENSIONS = {".bmp", ".jpg", ".jpeg", ".png"}
OFFICIAL_FEATURED = (
    ("樱花树下的少女", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm7.png"),
    ("日落", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm8.png"),
    ("云朵与少女", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm5.png"),
    ("无缝风铃蝴蝶房屋独寂", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm4.png"),
    ("中国故宫少女", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm6.png"),
)
OFFICIAL_SHOWCASE_URL = "https://www.yuanqidesk.com/"
OFFICIAL_CATALOG_URL = "https://bizhi.cheetahfun.com/"
OFFICIAL_FEATURED_SOURCE = "元气官网首页公开展示"
LEGACY_FEATURED_SOURCES = {"元气官网公开精选", "元气官网精选", OFFICIAL_FEATURED_SOURCE}
YUANQI_CACHE_SOURCE = "本机元气静态缓存（已独立复制）"
DEFAULT_YUANQI_CACHE_DIRS = (
    Path.home() / "AppData" / "Roaming" / "kdesk" / "data" / "wallpaper" / "img",
    Path("G:/元气壁纸缓存/img"),
    Path("G:/元气壁纸缓存/khealtheye/wallpaper"),
)
OFFICIAL_STATIC_CATEGORIES = (
    ("全部静态", "https://bizhi.cheetahfun.com/dn/j/"),
    ("4K", "https://bizhi.cheetahfun.com/tag_93_07530310f9c29fbc88118c6eb8bb2047}/"),
    ("风景", "https://bizhi.cheetahfun.com/dn/c1j/"),
    ("小清新", "https://bizhi.cheetahfun.com/dn/c10j/"),
    ("动漫", "https://bizhi.cheetahfun.com/dn/c2j/"),
    ("明星", "https://bizhi.cheetahfun.com/dn/c5j/"),
    ("美女", "https://bizhi.cheetahfun.com/dn/c3j/"),
    ("科幻", "https://bizhi.cheetahfun.com/dn/c7j/"),
    ("动物", "https://bizhi.cheetahfun.com/dn/c6j/"),
    ("游戏", "https://bizhi.cheetahfun.com/dn/c4j/"),
    ("体育", "https://bizhi.cheetahfun.com/dn/c9j/"),
    ("汽车", "https://bizhi.cheetahfun.com/dn/c8j/"),
    ("其他", "https://bizhi.cheetahfun.com/dn/c11j/"),
)
SPI_SETDESKWALLPAPER = 20
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
LOCAL_PAGE_SIZE = 12
BG, PANEL, TEXT, MUTED, GOLD, PINK, LINE = "#ffffff", "#ffffff", "#171b27", "#7d8493", "#ffc400", "#ff4f8b", "#edf0f4"
NAV_BG, SOFT_BG, SELECT_BG = "#f3f4f6", "#f7f8fa", "#fff5cc"


@dataclass
class Wallpaper:
    id: str
    path: str
    title: str
    favorite: bool = False
    source: str = "本地导入"


def wallpaper_matches_query(item: Wallpaper, query: str) -> bool:
    """本地图库搜索与顶栏全局搜索共用的匹配规则。"""
    terms = tuple(part for part in query.casefold().split() if part)
    if not terms:
        return True
    haystack = " ".join((item.title, item.source, Path(item.path).stem)).casefold()
    return all(term in haystack for term in terms)


def load_config() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        return {"items": [], "interval_minutes": 0, "last_applied": ""}
    try:
        return normalize_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {"items": [], "interval_minutes": 0, "last_applied": ""}


def save_config(config: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_config(config: dict) -> dict:
    """迁移旧版来源名称，并只对官网 5 张公开样图去重。"""
    normalized = dict(config) if isinstance(config, dict) else {}
    normalized_items: list[dict] = []
    featured_titles = {title for title, _url in OFFICIAL_FEATURED}
    seen_featured: set[str] = set()
    id_aliases: dict[str, str] = {}
    for raw in normalized.get("items", []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        title = str(item.get("title", ""))
        source = str(item.get("source", "本地导入"))
        if title in featured_titles and source in LEGACY_FEATURED_SOURCES:
            item["source"] = OFFICIAL_FEATURED_SOURCE
            if title in seen_featured:
                if item.get("id") and normalized_items:
                    kept = next((entry for entry in normalized_items if entry.get("title") == title and entry.get("source") == OFFICIAL_FEATURED_SOURCE), None)
                    if kept and kept.get("id"):
                        id_aliases[str(item["id"])] = str(kept["id"])
                continue
            seen_featured.add(title)
        normalized_items.append(item)
    normalized["items"] = normalized_items
    last_applied = str(normalized.get("last_applied", ""))
    if last_applied in id_aliases:
        normalized["last_applied"] = id_aliases[last_applied]
    normalized.setdefault("interval_minutes", 0)
    normalized.setdefault("last_applied", "")
    return normalized


def current_native_wallpaper() -> Path:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
        value, _kind = winreg.QueryValueEx(key, "WallPaper")
    return Path(value)


def same_windows_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_valid_static_image(path: Path) -> bool:
    if path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with Image.open(path) as image:
            if getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1:
                return False
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def discover_yuanqi_cache_images(roots: tuple[Path, ...] | list[Path]) -> list[Path]:
    discovered: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if not is_valid_static_image(candidate):
                continue
            fingerprint = file_digest(candidate)
            if fingerprint not in seen:
                seen.add(fingerprint)
                discovered.append(candidate)
    return discovered


def copy_yuanqi_cache_images(candidates: list[Path], existing_paths: list[Path]) -> list[Wallpaper]:
    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    existing_hashes = {file_digest(path) for path in existing_paths if is_valid_static_image(path)}
    imported: list[Wallpaper] = []
    for candidate in candidates:
        fingerprint = file_digest(candidate)
        if fingerprint in existing_hashes:
            continue
        suffix = candidate.suffix.lower()
        target = WALLPAPER_DIR / f"{uuid.uuid4().hex}{suffix}"
        shutil.copy2(candidate, target)
        try:
            with Image.open(target) as image:
                width, height = image.size
        except OSError:
            target.unlink(missing_ok=True)
            continue
        short_id = fingerprint[:8]
        imported.append(Wallpaper(uuid.uuid4().hex, str(target), f"元气静态壁纸 {width}×{height} · {short_id}", source=YUANQI_CACHE_SOURCE))
        existing_hashes.add(fingerprint)
    return imported


def set_native_wallpaper(path: Path) -> None:
    path = path.resolve()
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("Windows 原生壁纸仅支持 BMP/JPG/PNG 图片")
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("壁纸文件不存在或为空")
    if not ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, str(path), SPIF_UPDATEINIFILE
    ):
        raise ctypes.WinError()
    applied = current_native_wallpaper()
    if not same_windows_path(applied, path):
        raise OSError(f"Windows 未确认壁纸变更，当前仍为：{applied}")


def download_static_image(url: str, title: str, source: str) -> Wallpaper:
    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if parsed.scheme != "https" or suffix not in IMAGE_EXTENSIONS:
        raise ValueError("只接受明确指向 BMP/JPG/JPEG/PNG 的 HTTPS 图片直链")
    target = WALLPAPER_DIR / f"{uuid.uuid4().hex}{suffix}"
    request = urllib.request.Request(url, headers={"User-Agent": "NativeWallpaperManager/0.3"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, target.open("xb") as output:
            if not response.headers.get_content_type().startswith("image/"):
                raise ValueError("服务器没有返回图片内容")
            shutil.copyfileobj(response, output)
        if target.stat().st_size == 0:
            raise ValueError("下载得到空文件")
        with Image.open(target) as downloaded:
            downloaded.verify()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return Wallpaper(uuid.uuid4().hex, str(target), title, source=source)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("桌面画廊 · 原生壁纸管理器")
        self.root.geometry("1180x760")
        self.root.minsize(1000, 640)
        self.root.configure(bg=BG)
        self.config = load_config()
        self.wechat_backend = WechatQrLoginBackend()
        self.yuanqi_auth = YuanqiAuthCoordinator(self.wechat_backend)
        self.yuanqi_source = YuanqiResourceSource(
            session_provider=self._wechat_session_provider,
            device_provider=self._wechat_device_provider,
        )
        self.yuanqi_dynamic_source = YuanqiDynamicResourceSource(
            session_provider=self._wechat_session_provider,
            device_provider=self._wechat_device_provider,
        )
        self.online_resource_mode = "static"
        self.online_photo_refs: list[ImageTk.PhotoImage] = []
        self.online_items: list[CatalogItem] = []
        self.online_selected_id: str | None = None
        self.online_cate_id = "__featured__"
        self.online_page = 1
        self.online_query = ""
        self.online_has_more = False
        self.online_total = 0
        self._online_cache: dict[tuple[str, str, int, str], object] = {}
        self._thumb_cache: dict[str, bytes] = {}
        self._thumb_cache_lock = threading.Lock()
        self._thumbnail_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="wallpaper-preview")
        self._thumbnail_futures: set[Future] = set()
        self._thumbnail_futures_lock = threading.Lock()
        self._preview_generation = 0
        self._online_card_widgets: dict[str, tk.Frame] = {}
        self._online_image_widgets: dict[str, tk.Label] = {}
        self._loading_online = False
        self._ui_queue: queue.Queue = queue.Queue()
        self.root.after(80, self._poll_ui_queue)
        threading.Thread(target=self._try_restore_session, daemon=True).start()
        self.items = [Wallpaper(**item) for item in self.config.get("items", []) if Path(item["path"]).exists()]
        self.filter_favorites = False
        self.local_query = ""
        self.local_page = 0
        self._current_page = "wallpaper"
        self.selected_id = self.config.get("last_applied") or (self.items[0].id if self.items else None)
        self.timer: threading.Timer | None = None
        self.status = tk.StringVar(value="本地 Windows 原生功能已就绪；元气账号尚未接入本客户端。")
        self.photo_refs: list[ImageTk.PhotoImage] = []
        self._build()
        self.refresh()
        self.schedule_next()
        self._init_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def button(self, parent, text, command, *, accent=False, width=None, compact=False):
        return tk.Button(parent, text=text, command=command, width=width, relief="flat", cursor="hand2",
                         bg=GOLD if accent else NAV_BG, fg=TEXT, activebackground="#ffd84c" if accent else "#e8eaee",
                         activeforeground=TEXT, font=("Microsoft YaHei UI", 10, "bold"),
                         padx=10 if compact else 14, pady=5 if compact else 9, bd=0)

    def _build(self) -> None:
        self.root.geometry("1280x820")
        self.root.minsize(1060, 680)
        header = tk.Frame(self.root, bg=PANEL, height=88)
        header.pack(fill="x"); header.pack_propagate(False)
        brand = tk.Frame(header, bg=PANEL); brand.pack(side="left", padx=(22, 18))
        tk.Label(brand, text="桌面画廊", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(brand, text="Windows 原生模式", bg=PANEL, fg=PINK, font=("Microsoft YaHei UI", 8, "bold")).pack(anchor="w")
        nav = tk.Frame(header, bg=NAV_BG, padx=4, pady=4); nav.pack(side="left", pady=14)
        self.nav_buttons = {}
        for key, label in (("wallpaper", "静态壁纸"), ("official", "资源接入"), ("theme", "电脑主题"), ("beauty", "系统美化"), ("components", "组件"), ("mine", "我的")):
            button = tk.Button(nav, text=label, command=lambda name=key: self.show_page(name), relief="flat", cursor="hand2", bd=0,
                               bg=NAV_BG, fg=TEXT, activebackground=GOLD, font=("Microsoft YaHei UI", 9, "bold"), padx=12, pady=12)
            button.pack(side="left", padx=1); self.nav_buttons[key] = button

        search_shell = tk.Frame(header, bg=PANEL, highlightbackground="#b9c0cc", highlightthickness=1)
        search_shell.pack(side="right", padx=(10, 20), pady=23)
        tk.Label(search_shell, text="全局", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 8, "bold")).pack(side="left", padx=(10, 0))
        self.global_search_var = tk.StringVar()
        self.global_search_entry = tk.Entry(search_shell, textvariable=self.global_search_var, width=16, relief="flat", bd=0,
                                            bg=PANEL, fg=TEXT, insertbackground=TEXT,
                                            font=("Microsoft YaHei UI", 10))
        self.global_search_entry.pack(side="left", padx=(12, 4), ipady=5)
        self.global_search_entry.bind("<Return>", lambda _event: self.run_global_search())
        tk.Button(search_shell, text="⌕", command=self.run_global_search, relief="flat", bd=0, cursor="hand2",
                  bg=PANEL, fg=MUTED, activebackground=PANEL, font=("Microsoft YaHei UI", 15, "bold"),
                  padx=8, pady=0).pack(side="right")

        body = tk.Frame(self.root, bg=BG)
        self.library_page = body
        main = tk.Frame(body, bg=BG); main.pack(fill="both", expand=True)
        local_tabs = tk.Frame(main, bg=BG); local_tabs.pack(fill="x", pady=(1, 8))
        self.all_button = self.button(local_tabs, "全部", self.show_all, compact=True); self.all_button.pack(side="left", padx=(0, 4))
        self.favorite_button = self.button(local_tabs, "收藏", self.show_favorites, compact=True); self.favorite_button.pack(side="left", padx=4)
        tk.Label(local_tabs, text="本机静态图库", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10)

        gallery_header = tk.Frame(main, bg=BG); gallery_header.pack(fill="x", pady=(0, 7))
        self.count_label = tk.Label(gallery_header, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)); self.count_label.pack(side="left", padx=10)
        self.button(gallery_header, "下一页", self.next_local_page, compact=True).pack(side="right", padx=(8, 2))
        self.local_page_label = tk.Label(gallery_header, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.local_page_label.pack(side="right", padx=5)
        self.button(gallery_header, "上一页", self.previous_local_page, compact=True).pack(side="right", padx=2)
        self.gallery_canvas = tk.Canvas(main, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(main, orient="vertical", command=self.gallery_canvas.yview)
        self.gallery_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y"); self.gallery_canvas.pack(fill="both", expand=True)
        self.gallery = tk.Frame(self.gallery_canvas, bg=BG)
        self.gallery_window = self.gallery_canvas.create_window((0, 0), window=self.gallery, anchor="nw")
        self.gallery.bind("<Configure>", lambda _event: self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all")))
        self.gallery_canvas.bind("<Configure>", lambda event: self.gallery_canvas.itemconfigure(self.gallery_window, width=event.width))
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.theme_page = self.build_theme_page()
        self.official_page = self.build_official_page()
        self.beauty_page = self.build_beauty_page()
        self.components_page = self.build_components_page()
        self.mine_page = self.build_mine_page()
        self.pages = {"wallpaper": self.library_page, "official": self.official_page, "theme": self.theme_page, "beauty": self.beauty_page, "components": self.components_page, "mine": self.mine_page}

        action_bar = tk.Frame(self.root, bg=PANEL, height=58, highlightbackground=LINE, highlightthickness=1)
        action_bar.pack(fill="x", side="bottom"); action_bar.pack_propagate(False)
        self.button(action_bar, "▶  自动换壁纸", self.open_settings, accent=True, compact=True).pack(side="left", padx=(18, 8), pady=10)
        self.button(action_bar, "▧  导入壁纸", self.import_images, compact=True).pack(side="left", padx=4, pady=10)
        self.button(action_bar, "直链下载", self.download_url, compact=True).pack(side="left", padx=4, pady=10)
        self.button(action_bar, "同步公开样图", self.sync_official_featured, compact=True).pack(side="left", padx=4, pady=10)
        self.local_action_group = tk.Frame(action_bar, bg=PANEL)
        self.local_action_group.pack(side="right", padx=14, pady=8)
        self.favorite_action = self.button(self.local_action_group, "☆  收藏", self.toggle_selected_favorite, compact=True)
        self.favorite_action.pack(side="left", padx=4)
        self.apply_button = self.button(self.local_action_group, "设为壁纸", self.apply_selected, accent=True, compact=True)
        self.apply_button.pack(side="left", padx=4)
        self.online_action_group = tk.Frame(action_bar, bg=PANEL)
        self.online_download_button = self.button(self.online_action_group, "下载到本地", self.download_online_selected, compact=True)
        self.online_download_button.pack(side="left", padx=4)
        self.online_apply_button = self.button(self.online_action_group, "下载并设为壁纸", self.apply_online_selected, accent=True, compact=True)
        self.online_apply_button.pack(side="left", padx=4)

        tk.Label(self.root, textvariable=self.status, bg="#eef0f5", fg="#53607a", anchor="w", padx=24, pady=10,
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", side="bottom")
        self.show_page("wallpaper")

    def show_page(self, name: str) -> None:
        self._current_page = name
        for page in self.pages.values(): page.pack_forget()
        self.pages[name].pack(fill="both", expand=True, padx=18, pady=(4, 8))
        for key, button in self.nav_buttons.items():
            button.configure(bg=GOLD if key == name else NAV_BG)
        self.local_action_group.pack_forget()
        self.online_action_group.pack_forget()
        if name == "wallpaper":
            self.local_action_group.pack(side="right", padx=14, pady=8)
        elif name == "official":
            self.online_action_group.pack(side="right", padx=14, pady=8)
        if name == "theme": self.refresh_theme_preview()
        if name == "mine": self.refresh_mine_summary()
        if name == "official":
            self.login_status.configure(text=self._login_status_text())
            self.load_online_items(self.online_cate_id, self.online_page, self.online_query)

    def _on_mousewheel(self, event) -> None:
        """按当前活动页面把滚轮路由到对应 canvas，避免本地/联网图库滚轮错位。"""
        delta = int(-event.delta / 120)
        canvas = getattr(self, "online_canvas", None) if self._current_page == "official" else getattr(self, "gallery_canvas", None)
        if canvas is not None:
            canvas.yview_scroll(delta, "units")

    def run_global_search(self) -> None:
        """从任意主分区进入统一结果页；搜索不继承当前分类。"""
        query = self.global_search_var.get().strip()
        if not query:
            self.clear_search()
            self.global_search_entry.focus_set()
            self.status.set("请输入壁纸名称、分类、作者或来源关键词。")
            return
        self.local_query = query
        self.filter_favorites = False
        self.local_page = 0
        self.search_var.set(query)
        self.online_query = query
        self.online_cate_id = "__featured__"
        self.online_page = 1
        self.render_global_local_matches(query)
        self.show_page("official")
        self.status.set(f"正在全局搜索“{query}”：本地图库 + 元气联网全部分类。")

    def render_global_local_matches(self, query: str) -> None:
        if not hasattr(self, "global_local_results"):
            return
        for child in self.global_local_results.winfo_children():
            child.destroy()
        matches = [item for item in self.items if wallpaper_matches_query(item, query)]
        if not query:
            self.global_local_results.pack_forget()
            return
        self.global_local_results.pack(fill="x", pady=(0, 8), before=self.online_body)
        tk.Label(self.global_local_results, text=f"本地匹配  {len(matches)} 张", bg=SOFT_BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=(12, 10), pady=8)
        for item in matches[:5]:
            tk.Button(self.global_local_results, text=item.title, command=lambda current=item: self.open_local_search_result(current),
                      relief="flat", bd=0, cursor="hand2", bg=PANEL, fg=TEXT, activebackground=SELECT_BG,
                      font=("Microsoft YaHei UI", 9), padx=10, pady=5).pack(side="left", padx=3, pady=6)
        if len(matches) > 5:
            tk.Label(self.global_local_results, text=f"另有 {len(matches) - 5} 张", bg=SOFT_BG, fg=MUTED,
                     font=("Microsoft YaHei UI", 9)).pack(side="left", padx=8)

    def open_local_search_result(self, item: Wallpaper) -> None:
        self.selected_id = item.id
        self.local_query = self.global_search_var.get().strip()
        matches = [entry for entry in self.visible_items()]
        try:
            index = next(index for index, entry in enumerate(matches) if entry.id == item.id)
        except StopIteration:
            index = 0
        self.local_page = index // LOCAL_PAGE_SIZE
        self.show_page("wallpaper")
        self.refresh()

    def page_header(self, page: tk.Frame, title: str, description: str) -> None:
        tk.Label(page, text=title, bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w", pady=(6, 2))
        tk.Label(page, text=description, bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(0, 18))

    def native_card(self, parent: tk.Frame, title: str, description: str, button_text: str, command) -> tk.Frame:
        card = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=22, pady=20)
        tk.Label(card, text=title, bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        tk.Label(card, text=description, bg=PANEL, fg=MUTED, justify="left", wraplength=340, font=("Microsoft YaHei UI", 9), pady=8).pack(anchor="w")
        self.button(card, button_text, command, accent=True).pack(anchor="w", pady=(6, 0))
        return card

    def build_theme_page(self) -> tk.Frame:
        page = tk.Frame(self.root, bg=BG); self.page_header(page, "电脑主题", "生成并应用 Windows 标准 .theme 文件；没有桌面覆盖层。")
        content = tk.Frame(page, bg=BG); content.pack(fill="both", expand=True)
        preview = tk.Frame(content, bg=PANEL, padx=22, pady=22); preview.pack(side="left", fill="both", expand=True, padx=(0, 14))
        tk.Label(preview, text="主题预览", bg=PANEL, fg=PINK, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        self.theme_image = tk.Label(preview, bg="#d8dce6"); self.theme_image.pack(fill="both", expand=True, pady=12)
        self.theme_title = tk.Label(preview, text="", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 16, "bold")); self.theme_title.pack(anchor="w")
        tk.Label(preview, text="包含当前静态壁纸与 Windows 原生图片幻灯片配置。", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=5)
        actions = tk.Frame(content, bg=BG, width=360); actions.pack(side="right", fill="y")
        self.native_card(actions, "生成 Windows 主题", "在本地创建可双击应用的 .theme 文件，图片轮换由 Windows 管理。", "生成主题文件", self.create_native_theme).pack(fill="x", pady=(0, 12))
        self.native_card(actions, "应用当前主题", "交给 Windows 个性化机制应用；仅改变主题和壁纸，不创建桌面层。", "用 Windows 应用", self.apply_native_theme).pack(fill="x")
        return page


    def _wechat_session_provider(self):
        try:
            return self.wechat_backend.get_user_info()
        except RuntimeError:
            return {}

    def _wechat_device_provider(self):
        try:
            return self.wechat_backend.get_device_info()
        except RuntimeError:
            return {}

    def build_official_page(self) -> tk.Frame:
        page = tk.Frame(self.root, bg=BG)
        mode_row = tk.Frame(page, bg=BG); mode_row.pack(fill="x", pady=(2, 4))
        self.online_mode_buttons: dict[str, tk.Button] = {}
        for mode, label in (("static", "静态壁纸"), ("dynamic", "动态转静态")):
            button = tk.Button(
                mode_row,
                text=label,
                command=lambda current=mode: self._switch_resource_mode(current),
                relief="flat",
                cursor="hand2",
                bd=0,
                bg=GOLD if mode == self.online_resource_mode else NAV_BG,
                fg=TEXT,
                activebackground=GOLD,
                font=("Microsoft YaHei UI", 10, "bold"),
                padx=15,
                pady=7,
            )
            button.pack(side="left", padx=(0, 4))
            self.online_mode_buttons[mode] = button
        self.dynamic_mode_notice = tk.Label(
            mode_row,
            text="动态资源仅解码 preview_video，多点选帧后生成原生 JPEG；不接入 CWP/加密视频。",
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 8),
        )
        self.dynamic_mode_notice.pack(side="left", padx=10)
        category_row = tk.Frame(page, bg=BG); category_row.pack(fill="x", pady=(2, 5))
        self.online_category_row = category_row
        self.online_cate_buttons: dict[str, tk.Button] = {}
        self._render_online_categories(self.yuanqi_source.list_categories())

        account_row = tk.Frame(page, bg=BG); account_row.pack(fill="x", pady=(0, 6))
        self.login_status = tk.Label(account_row, text=self._login_status_text(), bg=BG, fg=PINK,
                                     font=("Microsoft YaHei UI", 9, "bold"))
        self.login_status.pack(side="left")
        tk.Label(account_row, text="搜索范围：本地图库 + 元气联网全部分类", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=14)
        status_actions = tk.Frame(account_row, bg=BG); status_actions.pack(side="right")
        self.button(status_actions, "微信扫码登录", self.start_wechat_login, accent=True, compact=True).pack(side="left", padx=(0, 6))
        self.logout_button = self.button(status_actions, "退出登录", self.logout_wechat)
        self.logout_button.pack(side="left", padx=(0, 6))
        self.logout_button.configure(state="disabled" if self.yuanqi_auth.snapshot.phase != AuthPhase.AUTHENTICATED else "normal")

        self.search_var = self.global_search_var
        self.online_search_hint = tk.Label(account_row, text="", bg=BG, fg=PINK, font=("Microsoft YaHei UI", 9, "bold"))
        self.online_search_hint.pack(side="left", padx=10)
        self.cache_status = tk.Label(account_row, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8))
        self.cache_status.pack(side="left", padx=6)
        self.button(status_actions, "迁移缓存", self.import_detected_yuanqi_cache, compact=True).pack(side="left", padx=(0, 4))
        self.button(status_actions, "选择目录", self.choose_yuanqi_cache_directory, compact=True).pack(side="left", padx=(0, 4))
        self.button(status_actions, "官网", self.open_official_site, compact=True).pack(side="left", padx=(0, 4))

        self.global_local_results = tk.Frame(page, bg=SOFT_BG)
        body = tk.Frame(page, bg=BG); body.pack(fill="both", expand=True)
        self.online_body = body
        grid_pane = tk.Frame(body, bg=BG); grid_pane.pack(side="left", fill="both", expand=True)
        top = tk.Frame(grid_pane, bg=BG); top.pack(fill="x", pady=(0, 6))
        self.online_page_label = tk.Label(top, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.online_page_label.pack(side="left")
        self.online_next_button = self.button(top, "下一页", lambda: self.load_online_items(self.online_cate_id, self.online_page + 1, self.online_query), compact=True)
        self.online_next_button.pack(side="right", padx=2)
        self.online_previous_button = self.button(top, "上一页", lambda: self.load_online_items(self.online_cate_id, max(1, self.online_page - 1), self.online_query), compact=True)
        self.online_previous_button.pack(side="right", padx=2)

        self.online_canvas = tk.Canvas(grid_pane, bg=BG, highlightthickness=0)
        online_scroll = tk.Scrollbar(grid_pane, orient="vertical", command=self.online_canvas.yview)
        self.online_canvas.configure(yscrollcommand=online_scroll.set)
        online_scroll.pack(side="right", fill="y"); self.online_canvas.pack(fill="both", expand=True)
        self.online_grid = tk.Frame(self.online_canvas, bg=BG)
        self.online_grid_window = self.online_canvas.create_window((0, 0), window=self.online_grid, anchor="nw")
        self.online_grid.bind("<Configure>", lambda _event: self.online_canvas.configure(scrollregion=self.online_canvas.bbox("all")))
        self.online_canvas.bind("<Configure>", lambda event: self.online_canvas.itemconfigure(self.online_grid_window, width=event.width))

        self.load_online_items("__featured__", 1)
        return page

    def _render_online_categories(self, categories) -> None:
        """用当前资源服务返回的导航顺序重绘分类栏。"""
        for child in self.online_category_row.winfo_children():
            child.destroy()
        self.online_cate_buttons = {}
        for cat in categories:
            button = tk.Button(
                self.online_category_row,
                text=cat.title,
                command=lambda c=cat.id: self._switch_online_category(c),
                relief="flat",
                cursor="hand2",
                bd=0,
                bg=BG,
                fg=TEXT,
                activebackground=SELECT_BG,
                font=("Microsoft YaHei UI", 10, "bold"),
                padx=10,
                pady=6,
            )
            button.pack(side="left", padx=1)
            self.online_cate_buttons[cat.id] = button
        for cid, button in self.online_cate_buttons.items():
            selected = not self.online_query and cid == self.online_cate_id
            button.configure(bg=PINK if selected else BG, fg=PANEL if selected else TEXT)

    def _active_online_source(self):
        return self.yuanqi_dynamic_source if self.online_resource_mode == "dynamic" else self.yuanqi_source

    def _source_for_item(self, item: CatalogItem):
        return self.yuanqi_dynamic_source if item.resource_kind == "dynamic" else self.yuanqi_source

    @staticmethod
    def _catalog_item_key(item: CatalogItem) -> str:
        return f"{item.resource_kind}:{item.id}"

    def _switch_resource_mode(self, mode: str) -> None:
        if mode not in {"static", "dynamic"}:
            return
        self.online_resource_mode = mode
        for key, button in self.online_mode_buttons.items():
            button.configure(bg=GOLD if key == mode else NAV_BG)
        self.global_search_var.set("")
        self.local_query = ""
        self.online_query = ""
        self.online_cate_id = "__featured__"
        self.online_page = 1
        self.online_search_hint.configure(text="")
        self.render_global_local_matches("")
        self._render_online_categories(self._active_online_source().list_categories())
        self.load_online_items("__featured__", 1)

    def _switch_online_category(self, cate_id: str) -> None:
        self.global_search_var.set("")
        self.local_query = ""
        self.online_query = ""
        self.online_search_hint.configure(text="")
        self.render_global_local_matches("")
        self.load_online_items(cate_id, 1)

    def search_online(self) -> None:
        self.run_global_search()

    def clear_search(self, *, load_online: bool = True) -> None:
        self.global_search_var.set("")
        self.local_query = ""
        self.online_query = ""
        self.online_search_hint.configure(text="")
        self.render_global_local_matches("")
        if load_online:
            self.load_online_items(self.online_cate_id, 1)

    def _safe_after(self, callback) -> None:
        """从后台线程把 UI 更新投递到主线程队列；线程安全。"""
        self._ui_queue.put(callback)

    def _make_tray_image(self) -> Image.Image:
        image = Image.new("RGBA", (64, 64), "#f4b734")
        draw = ImageDraw.Draw(image)
        draw.rectangle([5, 5, 59, 59], outline="#8a6d1a", width=3)
        draw.rectangle([12, 20, 52, 52], outline="#5a4a12", width=2)
        draw.polygon([(16, 46), (30, 30), (38, 38), (46, 26), (48, 46)], fill="#ffffff")
        draw.ellipse([34, 24, 42, 32], fill="#5a4a12")
        return image

    def _init_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("显示桌面画廊", self._tray_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        self.tray_icon = pystray.Icon("desktop_gallery", self._make_tray_image(), "桌面画廊 · 原生壁纸管理器", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _on_close(self) -> None:
        """点窗口关闭按钮时隐藏到系统托盘，不退出程序。"""
        self.root.withdraw()
        try:
            self.tray_icon.notify("桌面画廊仍在运行，双击托盘图标可重新打开。", "桌面画廊")
        except Exception:
            pass

    def _tray_show(self, *_args) -> None:
        self._safe_after(lambda: (self.root.deiconify(), self.root.lift(), self.root.focus_force()))

    def _tray_quit(self, *_args) -> None:
        self._safe_after(self._quit_app)

    def _quit_app(self) -> None:
        self._thumbnail_executor.shutdown(wait=False, cancel_futures=True)
        try:
            self.tray_icon.stop()
        except Exception:
            pass
        self.root.destroy()

    def _poll_ui_queue(self) -> None:
        """主线程每 80ms 排空 UI 更新队列，再重新调度自身。"""
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                try:
                    callback()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            if self.root.winfo_exists():
                self.root.after(80, self._poll_ui_queue)
        except Exception:
            pass

    def _login_status_text(self) -> str:
        if self.yuanqi_auth.snapshot.phase == AuthPhase.AUTHENTICATED:
            return "账号状态：已登录（微信扫码会话经资源服务校验通过）"
        return "账号状态：未登录（请点击“微信扫码登录”）"

    def logout_wechat(self) -> None:
        self.wechat_backend.clear()
        self.yuanqi_auth.clear()
        clear_session()
        self.yuanqi_source.reset_session_cache()
        self.yuanqi_dynamic_source.reset_session_cache()
        self._online_cache.clear()
        self.online_selected_id = None
        self.login_status.configure(text=self._login_status_text())
        self.logout_button.configure(state="disabled")
        self.load_online_items("__featured__", 1)
        self.status.set("已退出本客户端登录态；官方客户端登录不受影响。")

    def _try_restore_session(self) -> None:
        """启动时用本地 DPAPI 加密会话恢复登录态；服务端校验失败则清除。"""
        session = load_session()
        if not session:
            return
        token, open_id, device_id = session
        log_diag(f"尝试恢复本地会话 open_id={sha8(open_id)} token_len={len(token)}")
        self.wechat_backend.store_session(token, open_id, device_id)
        snapshot = self.yuanqi_auth.callback_on_login({"ret": 0})
        if snapshot.phase != AuthPhase.AUTHENTICATED:
            log_diag(f"本地会话恢复校验未通过 phase={snapshot.phase}")
            clear_session()
            return
        log_diag("本地会话恢复成功 AUTHENTICATED")
        self._safe_after(self._on_session_restored)

    def _on_session_restored(self) -> None:
        self.login_status.configure(text=self._login_status_text())
        self.logout_button.configure(state="normal")
        self._online_cache.clear()
        self.load_online_items("__featured__", 1)
        self.status.set("已从本地加密会话恢复登录状态。")


    def load_online_items(self, cate_id: str, page: int, query: str = "") -> None:
        self.online_cate_id = cate_id
        self.online_page = page
        self.online_query = query
        self.online_search_hint.configure(text=f"全局搜索：{query}" if query else "")
        self.online_previous_button.configure(state="disabled")
        self.online_next_button.configure(state="disabled")
        for cid, button in self.online_cate_buttons.items():
            selected = not query and cid == cate_id
            button.configure(bg=PINK if selected else BG, fg=PANEL if selected else TEXT)
        self._clear_online_grid()
        if self.yuanqi_auth.snapshot.phase != AuthPhase.AUTHENTICATED:
            self.online_page_label.configure(text="")
            self._online_grid_message("尚未登录。请先点击“微信扫码登录”。")
            return
        cache_mode = "all" if query else self.online_resource_mode
        key = (cache_mode, cate_id, page, query)
        cached = self._online_cache.get(key)
        if cached is not None:
            self._set_online_items(cached)
            return
        self._loading_online = True
        self.online_page_label.configure(text="正在加载……")
        self._online_grid_message("正在加载，请稍候……")
        threading.Thread(target=self._fetch_online, args=(cate_id, page, query), daemon=True).start()

    def _fetch_online(self, cate_id: str, page: int, query: str) -> None:
        try:
            source = self._active_online_source()
            categories = source.refresh_categories()
            self._safe_after(lambda current=categories: self._render_online_categories(current))
            if query:
                static_result = self.yuanqi_source.search(query, page)
                dynamic_result = self.yuanqi_dynamic_source.search(query, page)
                result = CatalogPage(
                    static_result.items + dynamic_result.items,
                    page,
                    static_result.has_more or dynamic_result.has_more,
                    static_result.total + dynamic_result.total,
                )
                cache_mode = "all"
            else:
                result = source.list_items(cate_id, page)
                cache_mode = self.online_resource_mode
            self._online_cache[(cache_mode, cate_id, page, query)] = result
            self._safe_after(lambda: self._set_online_items(result))
        except (IntegrationUnavailable, CatalogError) as exc:
            self._safe_after(lambda: self._online_error(str(exc)))
        finally:
            self._loading_online = False

    def _clear_grid_children(self) -> None:
        for child in self.online_grid.winfo_children():
            child.destroy()

    def _set_online_items(self, result) -> None:
        self._clear_grid_children()
        self.online_items = list(result.items)
        self.online_has_more = result.has_more
        self.online_total = result.total
        self.online_previous_button.configure(state="normal" if self.online_page > 1 else "disabled")
        self.online_next_button.configure(state="normal" if self.online_has_more else "disabled")
        if self.online_query:
            local_count = sum(wallpaper_matches_query(item, self.online_query) for item in self.items)
            static_count = sum(item.resource_kind == "static" for item in self.online_items)
            dynamic_count = sum(item.resource_kind == "dynamic" for item in self.online_items)
            label = (
                f"全局结果：本地 {local_count} 张 · 联网共 {result.total or 0} 张 "
                f"（本页静态 {static_count} / 动态 {dynamic_count}）· 第 {self.online_page} 页"
            )
        else:
            label = f"第 {self.online_page} 页 · 共 {result.total or '?'} 条"
        self.online_page_label.configure(text=label)
        if not self.online_items:
            if self.online_query:
                self._online_grid_message("联网全部分类中没有匹配结果；上方仍会保留本地图库匹配。")
            else:
                title = next((cat.title for cat in self._active_online_source().list_categories() if cat.id == self.online_cate_id), "当前分类")
                self._online_grid_message(f"{title}暂无可验证的匹配资源；桌面画廊不会用模拟数据补齐。")
            self.status.set("当前筛选没有可验证结果。")
            return
        self._render_online_grid_batched(0)
        self.online_canvas.yview_moveto(0)
        if self.online_items:
            self.select_online(self.online_items[0])
        self.status.set(f"联网壁纸已加载：{len(self.online_items)} 条。")

    def _online_error(self, message: str) -> None:
        self._clear_online_grid()
        self.online_page_label.configure(text="")
        self._online_grid_message(message)
        self.status.set(f"联网壁纸加载失败：{message}")

    def _online_grid_message(self, message: str) -> None:
        for child in self.online_grid.winfo_children():
            child.destroy()
        tk.Label(self.online_grid, text=message, bg=BG, fg=PINK, wraplength=460, justify="left",
                 font=("Microsoft YaHei UI", 11)).pack(pady=44)

    def _clear_online_grid(self) -> None:
        self._preview_generation += 1
        with self._thumbnail_futures_lock:
            for future in self._thumbnail_futures:
                future.cancel()
            self._thumbnail_futures.clear()
        for child in self.online_grid.winfo_children():
            child.destroy()
        self._online_card_widgets = {}
        self._online_image_widgets = {}
        self.online_photo_refs.clear()
        self.online_items = []
        self.online_selected_id = None
        if hasattr(self, "online_download_button"):
            self.online_download_button.configure(state="disabled")
            self.online_apply_button.configure(state="disabled")

    def _render_online_grid_batched(self, start: int = 0, generation: int | None = None) -> None:
        generation = self._preview_generation if generation is None else generation
        if generation != self._preview_generation:
            return
        batch = 6
        end = min(start + batch, len(self.online_items))
        for index in range(start, end):
            item = self.online_items[index]
            card = self._online_card(item)
            card.grid(row=index // 3, column=index % 3, padx=5, pady=5, sticky="nsew")
            self.online_grid.grid_columnconfigure(index % 3, weight=1)
            self._online_card_widgets[self._catalog_item_key(item)] = card
        if end < len(self.online_items):
            self.root.after_idle(lambda: self._render_online_grid_batched(end, generation))
        else:
            self._update_online_borders()

    def _online_card(self, item: CatalogItem) -> tk.Frame:
        selected = self._catalog_item_key(item) == self.online_selected_id
        card = tk.Frame(self.online_grid, bg=PANEL, highlightbackground=GOLD if selected else PANEL,
                        highlightthickness=3 if selected else 0, cursor="hand2")
        photo = self._placeholder_thumbnail((380, 214)); self.online_photo_refs.append(photo)
        picture = tk.Label(card, image=photo, bg=PANEL, cursor="hand2", bd=0); picture.pack(fill="x")
        picture._photo = photo
        item_key = self._catalog_item_key(item)
        self._online_image_widgets[item_key] = picture
        name = tk.Label(picture, text=item.title, bg="#20232a", fg="#ffffff", anchor="w",
                        font=("Microsoft YaHei UI", 9, "bold"), padx=8, pady=3)
        name.place(x=8, y=8)
        meta_text = "动态预览转静态" if item.resource_kind == "dynamic" else (item.category_name or "精选")
        if item.resolution:
            meta_text += " · " + item.resolution
        meta = tk.Label(card, text=meta_text, bg=PANEL, fg=MUTED, anchor="w", font=("Microsoft YaHei UI", 8))
        meta.pack(fill="x", padx=3, pady=(4, 2))
        for widget in (card, picture, name, meta):
            widget.bind("<Button-1>", lambda _event, current=item: self.select_online(current))
            widget.bind("<Double-Button-1>", lambda _event, current=item: self.show_online_detail(current))
        self._queue_online_thumbnail(item, picture, (380, 214), self._preview_generation)
        return card

    def _update_online_borders(self) -> None:
        for item_id, card in self._online_card_widgets.items():
            sel = item_id == self.online_selected_id
            card.configure(highlightbackground=GOLD if sel else PANEL, highlightthickness=3 if sel else 0)

    @staticmethod
    def _placeholder_thumbnail(size: tuple[int, int]) -> ImageTk.PhotoImage:
        image = Image.new("RGB", size, "#d8dce6")
        draw = ImageDraw.Draw(image)
        width, height = size
        draw.rectangle((width // 2 - 18, height // 2 - 12, width // 2 + 18, height // 2 + 12),
                       outline="#aeb5c2", width=2)
        return ImageTk.PhotoImage(image)

    def _fetch_online_thumbnail_image(self, item: CatalogItem, size: tuple[int, int]) -> Image.Image:
        with self._thumb_cache_lock:
            raw = self._thumb_cache.get(item.preview_url)
        if raw is None:
            try:
                response = requests.get(item.preview_url, timeout=20)
                response.raise_for_status()
                raw = response.content
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError("thumbnail exceeds limit")
            except (OSError, ValueError, requests.RequestException):
                raw = b""
        try:
            if raw:
                with Image.open(io.BytesIO(raw)) as decoded:
                    image = decoded.convert("RGB")
                    image.load()
                with self._thumb_cache_lock:
                    self._thumb_cache[item.preview_url] = raw
            else:
                raise ValueError("no cached bytes")
        except (OSError, ValueError):
            image = Image.new("RGB", size, "#d8dce6")
        return ImageOps.fit(image, size, Image.Resampling.LANCZOS)

    def _queue_online_thumbnail(
        self,
        item: CatalogItem,
        label: tk.Label,
        size: tuple[int, int],
        generation: int,
    ) -> None:
        item_key = self._catalog_item_key(item)
        try:
            future = self._thumbnail_executor.submit(self._fetch_online_thumbnail_image, item, size)
        except RuntimeError:
            return
        with self._thumbnail_futures_lock:
            self._thumbnail_futures.add(future)

        def completed(done: Future) -> None:
            with self._thumbnail_futures_lock:
                self._thumbnail_futures.discard(done)
            if done.cancelled():
                return
            try:
                image = done.result()
            except Exception:
                return
            self._safe_after(
                lambda current=image: self._apply_online_thumbnail(item_key, label, current, generation)
            )

        future.add_done_callback(completed)

    def _apply_online_thumbnail(
        self,
        item_key: str,
        label: tk.Label,
        image: Image.Image,
        generation: int,
    ) -> None:
        if generation != self._preview_generation or self._online_image_widgets.get(item_key) is not label:
            return
        try:
            if not label.winfo_exists():
                return
            photo = ImageTk.PhotoImage(image)
            label._photo = photo
            label.configure(image=photo)
            self.online_photo_refs.append(photo)
        except tk.TclError:
            return

    def online_thumbnail_for(self, item: CatalogItem, size: tuple[int, int]) -> ImageTk.PhotoImage:
        """兼容同步调用点；联网卡片本身使用三线程渐进加载。"""
        return ImageTk.PhotoImage(self._fetch_online_thumbnail_image(item, size))

    def selected_online(self) -> CatalogItem | None:
        return next((item for item in self.online_items if self._catalog_item_key(item) == self.online_selected_id), None)

    def select_online(self, item: CatalogItem) -> None:
        self.online_selected_id = self._catalog_item_key(item)
        self._update_online_borders()
        if hasattr(self, "online_download_button"):
            self.online_download_button.configure(state="normal")
            self.online_apply_button.configure(state="normal")
            dynamic = item.resource_kind == "dynamic"
            self.online_download_button.configure(text="转换到本地" if dynamic else "下载到本地")
            self.online_apply_button.configure(text="转换并设为壁纸" if dynamic else "下载并设为壁纸")
        suffix = "；将从预览视频多点选取高清帧。" if item.resource_kind == "dynamic" else "；双击卡片可查看大图。"
        self.status.set(f"已选择：{item.title}{suffix}")

    def show_online_detail(self, item: CatalogItem | None = None) -> None:
        item = item or self.selected_online()
        if not item:
            messagebox.showinfo("请选择壁纸", "请先在联网壁纸里点选一张。")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"壁纸详情 · {item.title}")
        dialog.geometry("920x640")
        dialog.configure(bg=PANEL)
        top = tk.Frame(dialog, bg=PANEL); top.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(top, text=item.title, bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 17, "bold")).pack(side="left")
        meta = " · ".join(part for part in (item.category_name, item.author, item.resolution, item.format) if part)
        tk.Label(top, text=meta, bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(side="left", padx=12)
        if item.resource_kind == "dynamic":
            tk.Label(
                dialog,
                text="画面预览仅用于浏览；转换时会从原始 preview_video 的 5 个时间点解码全分辨率帧并自动择优，不取首帧。",
                bg=PANEL,
                fg=PINK,
                anchor="w",
                font=("Microsoft YaHei UI", 9, "bold"),
            ).pack(fill="x", padx=16, pady=(0, 3))
        image_label = tk.Label(dialog, bg="#d8dce6"); image_label.pack(fill="both", expand=True, padx=16, pady=6)
        actions = tk.Frame(dialog, bg=PANEL); actions.pack(fill="x", padx=16, pady=(6, 14))
        download_label = "转换到本地图库" if item.resource_kind == "dynamic" else "下载到本地图库"
        apply_label = "转换并应用为壁纸" if item.resource_kind == "dynamic" else "下载并应用为壁纸"
        self.button(actions, download_label, lambda: self._download_item_to_library(item)).pack(side="left", padx=(0, 8))
        self.button(actions, apply_label, lambda: self._apply_item(item)).pack(side="left", padx=(0, 8))
        self.status.set(f"正在加载大图：{item.title}")
        threading.Thread(target=self._load_detail_image, args=(item, image_label), daemon=True).start()

    def _load_detail_image(self, item: CatalogItem, label: tk.Label) -> None:
        try:
            response = requests.get(item.original_url or item.preview_url, timeout=30)
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
        except (OSError, ValueError, requests.RequestException):
            image = Image.new("RGB", (820, 460), "#d8dce6")
        image.thumbnail((860, 480), Image.Resampling.LANCZOS)
        self._safe_after(lambda current=image: self._apply_detail_image(label, current))

    @staticmethod
    def _apply_detail_image(label: tk.Label, image: Image.Image) -> None:
        try:
            if not label.winfo_exists():
                return
            photo = ImageTk.PhotoImage(image)
            label._photo = photo  # 持有引用，防止被 GC
            label.configure(image=photo)
        except tk.TclError:
            return

    def _online_save_target(self, item: CatalogItem) -> Path:
        suffix = ".jpg" if item.resource_kind == "dynamic" else Path(item.original_url or "").suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            suffix = ".jpg"
        WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
        return WALLPAPER_DIR / f"{uuid.uuid4().hex}{suffix}"

    def download_online_selected(self) -> None:
        item = self.selected_online()
        if not item:
            messagebox.showinfo("请选择壁纸", "请先在联网壁纸里点选一张。")
            return
        self._download_item_to_library(item)

    def _download_item_to_library(self, item: CatalogItem) -> None:
        if item.resource_kind == "dynamic":
            self.status.set(f"正在下载动态预览并从 5 个时间点选择高清帧：{item.title}")
        else:
            self.status.set(f"正在下载：{item.title}")

        def work() -> None:
            target: Path | None = None
            try:
                source_adapter = self._source_for_item(item)
                data = source_adapter.download_original(item.id)
                target = self._online_save_target(item)
                target.write_bytes(data)
                if not is_valid_static_image(target):
                    raise CatalogError("生成文件没有通过静态图片校验")
                if item.resource_kind == "dynamic":
                    summary = source_adapter.conversion_summary(item.id)
                    source_name = f"元气动态预览转静态 · {summary}" if summary else "元气动态预览转静态"
                else:
                    source_name = "元气联网下载"
                wallpaper = Wallpaper(uuid.uuid4().hex, str(target), item.title, source=source_name)
                self._safe_after(lambda: self._finish_online_download(wallpaper, None))
            except (CatalogError, OSError, ValueError) as exc:
                if target is not None:
                    target.unlink(missing_ok=True)
                self._safe_after(lambda error=exc: self._finish_online_download(None, error))

        threading.Thread(target=work, daemon=True).start()

    def _finish_online_download(self, wallpaper: Wallpaper | None, error: Exception | None) -> None:
        if error:
            self.status.set(f"下载失败：{error}")
            messagebox.showerror("下载失败", str(error))
            return
        self.items.append(wallpaper)
        self.selected_id = wallpaper.id
        self.refresh()
        conversion = wallpaper.source if wallpaper.source.startswith("元气动态预览转静态") else ""
        self.status.set(f"已加入本地图库：{wallpaper.title}" + (f"（{conversion}）" if conversion else ""))
        detail = f"\n转换信息：{conversion}" if conversion else ""
        messagebox.showinfo("转换成功" if conversion else "下载成功", f"已把“{wallpaper.title}”加入本地图库。{detail}\n可到“本地壁纸”页预览或应用。")

    def apply_online_selected(self) -> None:
        item = self.selected_online()
        if not item:
            messagebox.showinfo("请选择壁纸", "请先在联网壁纸里点选一张。")
            return
        self._apply_item(item)

    def _apply_item(self, item: CatalogItem) -> None:
        if item.resource_kind == "dynamic":
            self.status.set(f"正在转换动态预览并交给 Windows 原生壁纸接口：{item.title}")
        else:
            self.status.set(f"正在下载并应用：{item.title}")

        def work() -> None:
            target: Path | None = None
            try:
                source_adapter = self._source_for_item(item)
                data = source_adapter.download_original(item.id)
                target = self._online_save_target(item)
                target.write_bytes(data)
                if not is_valid_static_image(target):
                    raise CatalogError("生成文件没有通过静态图片校验")
                set_native_wallpaper(target)
                if item.resource_kind == "dynamic":
                    summary = source_adapter.conversion_summary(item.id)
                    source_name = f"元气动态预览转静态 · {summary}" if summary else "元气动态预览转静态"
                else:
                    source_name = "元气联网下载"
                wallpaper = Wallpaper(uuid.uuid4().hex, str(target), item.title, source=source_name)
                self._safe_after(lambda: self._finish_online_apply(wallpaper, None))
            except (CatalogError, OSError, ValueError) as exc:
                if target is not None:
                    target.unlink(missing_ok=True)
                self._safe_after(lambda error=exc: self._finish_online_apply(None, error))

        threading.Thread(target=work, daemon=True).start()

    def _finish_online_apply(self, wallpaper: Wallpaper | None, error: Exception | None) -> None:
        if error:
            self.status.set(f"应用失败：{error}")
            messagebox.showerror("应用失败", str(error))
            return
        self.items.append(wallpaper)
        self.selected_id = wallpaper.id
        self.refresh()
        conversion = wallpaper.source if wallpaper.source.startswith("元气动态预览转静态") else ""
        self.status.set(f"已应用为 Windows 原生壁纸：{wallpaper.title}" + (f"（{conversion}）" if conversion else ""))
        detail = f"\n转换信息：{conversion}" if conversion else ""
        messagebox.showinfo("应用成功", f"“{wallpaper.title}”已由 Windows 注册表复核应用成功。{detail}")

    def _launch_wechat_login_window(self) -> dict | None:
        """以独立子进程拉起微信登录窗，读取其 stdout 返回的会话结构。"""
        project_root = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(project_root / "app" / "wechat_login_window.py")]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=720, encoding="utf-8")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.status.set(f"微信登录窗口异常：{exc}")
            return None
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None

    def start_wechat_login(self) -> None:
        """由本客户端发起微信扫码登录：弹出官方二维码窗口，等待本人扫码。"""
        self.status.set("正在弹出微信扫码登录窗口……")

        def work() -> None:
            result = self._launch_wechat_login_window()
            self._safe_after(lambda: self._finish_wechat_login(result))

        threading.Thread(target=work, daemon=True).start()
        messagebox.showinfo("微信扫码登录", "即将弹出官方微信登录窗口。\n请用微信扫描二维码并在手机端确认；确认后本客户端会自动读取会话并校验。")

    def _finish_wechat_login(self, result: dict | None) -> None:
        if not result or not result.get("ok"):
            message = (result or {}).get("message", "登录未完成。")
            self.status.set(f"微信登录未成功：{message}")
            messagebox.showwarning("微信登录", message)
            return
        session = result.get("session") or {}
        token = str(session.get("token") or "")
        open_id = str(session.get("open_id") or "")
        device_id = str(session.get("device_id") or "")
        log_diag(
            f"login window result ok=True verified={result.get('verified')} "
            f"open_id={sha8(open_id)} token_len={len(token)} device={sha8(device_id)}"
        )
        if not (token and open_id and device_id):
            log_diag("login window 会话不完整")
            self.status.set("微信登录返回的会话不完整。")
            messagebox.showerror("会话校验失败", "登录窗返回的会话缺少 token/open_id/device_id。")
            return
        self.wechat_backend.store_session(token, open_id, device_id)
        snapshot = self.yuanqi_auth.callback_on_login({"ret": 0})
        if snapshot.phase != AuthPhase.AUTHENTICATED:
            log_diag(f"app 会话校验未通过 phase={snapshot.phase} message={snapshot.message}")
            self.status.set(f"会话校验未通过：{snapshot.message}")
            messagebox.showerror("会话校验失败", f"{snapshot.message}\n\n请把“我的资料库\\AppData\\Local\\NativeWallpaperManager\\login_debug.log”内容发给我定位。")
            return
        log_diag("app 会话校验通过 AUTHENTICATED")
        try:
            save_session(token, open_id, device_id)
        except OSError:
            log_diag("保存本地加密会话失败")
        self.status.set("微信登录成功：账号、设备与资源服务会话均已由服务端校验。")
        self.login_status.configure(text=self._login_status_text())
        self.logout_button.configure(state="normal")
        self.yuanqi_source.reset_session_cache()
        self.yuanqi_dynamic_source.reset_session_cache()
        self._online_cache.clear()
        self.load_online_items("__featured__", 1)
        messagebox.showinfo("登录成功", "已用微信扫码登录元气账号，并已加密保存在本机。\n下次启动将自动保持登录。")

    def show_yuanqi_auth_status(self) -> None:
        methods = "\n".join(
            f"{method.channel} → {method.callback or '无回调'}"
            for method in BRIDGE_METHODS
        )
        options = " / ".join(option.label for option in LOGIN_OPTIONS)
        phone_flow = (
            f"{PHONE_LOGIN_SURFACE.phone_placeholder} → {PHONE_LOGIN_SURFACE.request_code_label} → "
            f"{PHONE_LOGIN_SURFACE.code_placeholder} → {PHONE_LOGIN_SURFACE.submit_label}"
        )
        wechat_flow = (
            f"{WECHAT_LOGIN_SURFACE.host_process} {' '.join(WECHAT_LOGIN_SURFACE.observed_arguments)} → "
            f"{WECHAT_LOGIN_SURFACE.qr_instruction} → 手机端确认"
        )
        wechat_return = (
            "统一登录宿主关闭 → SDK IUserInfoChangeListener → 主宿主刷新 → 会话校验"
            if WECHAT_AUTH_RETURN_SURFACE.external_host_closed
            else "等待官方宿主回调"
        )
        wechat_internal_boundary = (
            f"{KVIPGUI_LOGIN_BRIDGE_BOUNDARY.state_query_method} → "
            f"{KVIPGUI_LOGIN_BRIDGE_BOUNDARY.self_close_window_message}；"
            f"{KVIPGUI_LOGIN_BRIDGE_BOUNDARY.credential_ingress_method} 为官方凭据入口，不复刻"
        )
        qq_flow = (
            f"{QQ_LOGIN_SURFACE.window_title} / 默认扫码 / {QQ_LOGIN_SURFACE.password_login_label} / "
            f"授权方：{QQ_LOGIN_SURFACE.relying_party}"
        )
        qq_return = (
            f"授权窗关闭 → 头像替换登录按钮 → 出现“{QQ_AUTH_RETURN_SURFACE.authenticated_tab_label}” → "
            "桥回调/会话校验"
        )
        messagebox.showinfo(
            "元气电脑版登录桥自检",
            "已复刻并测试的原生桥契约：\n\n"
            f"{methods}\n\n"
            f"实机登录入口：{options}（默认微信二维码）。\n\n"
            f"微信登录：{wechat_flow}。\n\n"
            f"微信回流：{wechat_return}。\n\n"
            f"微信内部边界：{wechat_internal_boundary}。\n\n"
            f"手机登录：{phone_flow}。\n\n"
            f"QQ 登录：{qq_flow}。\n\n"
            f"QQ 回流：{qq_return}。\n\n"
            "状态流：拉起登录 → 选择入口/本人认证 → 登录回调 → 用户/设备信息 → 服务端校验。\n\n"
            f"当前：{self.yuanqi_auth.snapshot.message}",
        )

    def build_beauty_page(self) -> tk.Frame:
        page = tk.Frame(self.root, bg=BG); self.page_header(page, "系统美化", "只进入 Windows 原生设置，不注入任务栏、不覆盖桌面。")
        grid = tk.Frame(page, bg=BG); grid.pack(fill="both", expand=True)
        cards = (("鼠标指针", "使用 Windows 鼠标指针方案；主题文件也可以包含 .cur/.ani。", "打开鼠标设置", lambda: self.open_windows_uri("main.cpl")),
                 ("颜色与深浅模式", "使用 Windows 个性化颜色、深色或浅色模式。", "打开颜色设置", lambda: self.open_windows_uri("ms-settings:colors")),
                 ("系统声音", "使用 Windows 声音方案；主题文件可引用 .wav。", "打开声音设置", lambda: self.open_windows_uri("mmsys.cpl")),
                 ("通知与专注", "只使用 Windows 通知与专注设置；不做第三方进程弹窗拦截。", "打开通知设置", lambda: self.open_windows_uri("ms-settings:notifications")))
        for index, values in enumerate(cards): self.native_card(grid, *values).grid(row=index // 2, column=index % 2, sticky="nsew", padx=7, pady=7)
        grid.grid_columnconfigure((0, 1), weight=1)
        return page

    def build_components_page(self) -> tk.Frame:
        page = tk.Frame(self.root, bg=BG); self.page_header(page, "组件", "只保留 Windows 有稳定原生入口的项目。")
        grid = tk.Frame(page, bg=BG); grid.pack(fill="both", expand=True)
        self.native_card(grid, "锁屏图片", "Windows 原生锁屏设置。系统策略限制时由 Windows 自己提示。", "打开锁屏设置", lambda: self.open_windows_uri("ms-settings:lockscreen")).grid(row=0, column=0, sticky="nsew", padx=7, pady=7)
        self.native_card(grid, "桌面助手", "不实现。悬浮式图标整理会在远程桌面、分辨率切换时破坏 Explorer。", "查看原因", self.show_safety_notice).grid(row=0, column=1, sticky="nsew", padx=7, pady=7)
        self.native_card(grid, "弹窗拦截", "不实现全局拦截；仅通过 Windows 通知/专注模式管理提醒。", "查看原因", self.show_safety_notice).grid(row=1, column=0, sticky="nsew", padx=7, pady=7)
        self.native_card(grid, "桌面时钟", "不实现常驻桌面层；可使用 Windows 锁屏和小组件。", "查看原因", self.show_safety_notice).grid(row=1, column=1, sticky="nsew", padx=7, pady=7)
        grid.grid_columnconfigure((0, 1), weight=1)
        return page

    def build_mine_page(self) -> tk.Frame:
        page = tk.Frame(self.root, bg=BG); self.page_header(page, "我的", "本地资料库、收藏、系统设置。")
        self.mine_summary = tk.Label(page, text="", bg=PANEL, fg=TEXT, justify="left", anchor="nw", padx=24, pady=24, font=("Microsoft YaHei UI", 12))
        self.mine_summary.pack(fill="x")

        system = tk.Frame(page, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=22, pady=16)
        system.pack(fill="x", pady=(0, 12))
        tk.Label(system, text="系统", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        tk.Label(system, text="关闭窗口时最小化到系统托盘；右键托盘图标可恢复或退出。", bg=PANEL, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 8))
        row = tk.Frame(system, bg=PANEL); row.pack(anchor="w")
        self.autostart_button = self.button(row, "", self.toggle_autostart, accent=True)
        self.autostart_button.pack(side="left")
        tk.Label(row, text="随系统启动", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 10)).pack(side="left", padx=8)
        self.refresh_autostart_button()
        return page

    def refresh_autostart_button(self) -> None:
        enabled = autostart_enabled()
        self.autostart_button.configure(text="关闭开机自启" if enabled else "开启开机自启")

    def toggle_autostart(self) -> None:
        enabled = autostart_enabled()
        ok = set_autostart(not enabled)
        self.refresh_autostart_button()
        self.status.set("开机自启已开启。" if ok and not enabled else ("开机自启已关闭。" if ok else "设置开机自启失败。"))
        if not ok:
            messagebox.showerror("开机自启", "无法写入 HKCU 启动项，请检查权限。")

    def visible_items(self) -> list[Wallpaper]:
        return [
            item for item in self.items
            if (item.favorite or not self.filter_favorites) and wallpaper_matches_query(item, self.local_query)
        ]

    def persist(self) -> None:
        self.config["items"] = [asdict(item) for item in self.items]
        save_config(self.config)

    def image_for(self, path: Path, size: tuple[int, int]) -> ImageTk.PhotoImage:
        try:
            with Image.open(path) as raw:
                image = ImageOps.fit(raw.convert("RGB"), size, Image.Resampling.LANCZOS)
        except OSError:
            image = Image.new("RGB", size, "#d8dce6")
        return ImageTk.PhotoImage(image)

    def selected(self) -> Wallpaper | None:
        return next((item for item in self.items if item.id == self.selected_id), None)

    def select(self, item: Wallpaper) -> None:
        self.selected_id = item.id
        self.refresh()

    def refresh(self) -> None:
        filtered_items = self.visible_items()
        filtered_ids = {item.id for item in filtered_items}
        if self.selected_id not in filtered_ids:
            self.selected_id = filtered_items[0].id if filtered_items else None
        page_count = max(1, (len(filtered_items) + LOCAL_PAGE_SIZE - 1) // LOCAL_PAGE_SIZE)
        self.local_page = min(max(self.local_page, 0), page_count - 1)
        start = self.local_page * LOCAL_PAGE_SIZE
        items = filtered_items[start:start + LOCAL_PAGE_SIZE]
        self.photo_refs.clear()
        for child in self.gallery.winfo_children(): child.destroy()
        for index, item in enumerate(items):
            selected = item.id == self.selected_id
            card = tk.Frame(self.gallery, bg=PANEL, highlightbackground=GOLD if selected else PANEL,
                            highlightthickness=3 if selected else 0, cursor="hand2")
            card.grid(row=index // 3, column=index % 3, padx=5, pady=5, sticky="nsew")
            self.gallery.grid_columnconfigure(index % 3, weight=1)
            photo = self.image_for(Path(item.path), (380, 214)); self.photo_refs.append(photo)
            picture = tk.Label(card, image=photo, bg=PANEL, cursor="hand2", bd=0); picture.pack(fill="x")
            title = tk.Label(picture, text=item.title, bg="#20232a", fg="#ffffff", anchor="w", cursor="hand2",
                             font=("Microsoft YaHei UI", 9, "bold"), padx=8, pady=3)
            title.place(x=8, y=8)
            meta = tk.Label(card, text=("★  " if item.favorite else "") + item.source, bg=PANEL, fg=MUTED,
                            anchor="w", cursor="hand2", font=("Microsoft YaHei UI", 8), pady=3)
            meta.pack(fill="x", padx=3)
            for widget in (card, picture, title, meta): widget.bind("<Button-1>", lambda _event, current=item: self.select(current))
        scope = f"搜索“{self.local_query}” · " if self.local_query else ""
        self.count_label.configure(text=f"{scope}{len(filtered_items)} 张静态壁纸")
        self.local_page_label.configure(text=f"第 {self.local_page + 1} / {page_count} 页")
        item = self.selected()
        if item:
            self.favorite_action.configure(text="★  取消收藏" if item.favorite else "☆  收藏")
            self.apply_button.configure(state="normal")
            self.status.set(f"已选择：{item.title} · {item.source}")
        else:
            self.apply_button.configure(state="disabled")
            self.favorite_action.configure(state="disabled")
        if item:
            self.favorite_action.configure(state="normal")
        self.all_button.configure(bg=PINK if not self.filter_favorites else BG, fg=PANEL if not self.filter_favorites else TEXT)
        self.favorite_button.configure(bg=PINK if self.filter_favorites else BG, fg=PANEL if self.filter_favorites else TEXT)
        self.persist()
        self.root.after_idle(lambda: self.gallery_canvas.yview_moveto(0))

    def show_all(self) -> None:
        self.filter_favorites = False
        self.local_query = ""
        self.global_search_var.set("")
        self.local_page = 0
        self.refresh()

    def previous_local_page(self) -> None:
        if self.local_page > 0:
            self.local_page -= 1
            self.refresh()

    def next_local_page(self) -> None:
        page_count = max(1, (len(self.visible_items()) + LOCAL_PAGE_SIZE - 1) // LOCAL_PAGE_SIZE)
        if self.local_page + 1 < page_count:
            self.local_page += 1
            self.refresh()

    def refresh_theme_preview(self) -> None:
        item = self.selected()
        if not item:
            self.theme_image.configure(image=""); self.theme_title.configure(text="请先导入一张静态壁纸"); return
        photo = self.image_for(Path(item.path), (620, 340)); self.photo_refs.append(photo)
        self.theme_image.configure(image=photo); self.theme_title.configure(text=f"{item.title} · 原生主题")

    def theme_path(self) -> Path:
        return THEME_DIR / "桌面画廊-当前主题.theme"

    def create_native_theme(self) -> Path | None:
        item = self.selected()
        if not item:
            messagebox.showinfo("请选择壁纸", "请先在静态壁纸页选择一张图片。"); return None
        THEME_DIR.mkdir(parents=True, exist_ok=True)
        images = [entry for entry in self.items if Path(entry.path).exists()]
        lines = [
            "[Theme]", f"DisplayName=桌面画廊 - {item.title}", "",
            "[Control Panel\\Desktop]", f"Wallpaper={item.path}", "TileWallpaper=0", "WallpaperStyle=10", "",
            "[Slideshow]", "Interval=1800000", "Shuffle=1", f"ImagesRootPath={WALLPAPER_DIR}",
        ]
        for index, entry in enumerate(images): lines.append(f"Item{index}Path={entry.path}")
        self.theme_path().write_text("\r\n".join(lines) + "\r\n", encoding="utf-16")
        self.status.set(f"已生成 Windows 原生主题：{self.theme_path().name}")
        return self.theme_path()

    def apply_native_theme(self) -> None:
        path = self.create_native_theme()
        if not path: return
        try:
            os.startfile(path)
            self.status.set("已交给 Windows 应用主题；没有桌面覆盖层。")
        except OSError as exc:
            messagebox.showerror("应用失败", str(exc))

    def open_windows_uri(self, target: str) -> None:
        try:
            os.startfile(target)
            self.status.set("已打开 Windows 原生设置。")
        except OSError as exc:
            messagebox.showerror("无法打开 Windows 设置", str(exc))

    def show_safety_notice(self) -> None:
        messagebox.showinfo("稳定模式", "此功能需要桌面覆盖层或全局注入，远程桌面和分辨率变化时可能造成图标、右键或 Explorer 异常，因此稳定模式不实现。")

    def refresh_mine_summary(self) -> None:
        favorites = sum(item.favorite for item in self.items)
        online_downloaded = sum(item.source == "元气联网下载" for item in self.items)
        account = "已登录（微信扫码会话）" if self.yuanqi_auth.snapshot.phase == AuthPhase.AUTHENTICATED else "未登录"
        self.mine_summary.configure(text=f"本地资料库\n\n{len(self.items)} 张静态壁纸\n{favorites} 张收藏\n{online_downloaded} 张联网下载\n\n元气账号\n{account}\n\n主题文件\n{self.theme_path()}\n\n资源全部存放在本机，会话令牌只驻留内存，不保存元气 Cookie 或令牌。")

    def show_favorites(self) -> None:
        self.filter_favorites = True
        self.local_query = ""
        self.global_search_var.set("")
        self.local_page = 0
        self.refresh()

    def add_file(self, source: Path, source_name: str = "本地导入") -> None:
        if source.suffix.lower() not in IMAGE_EXTENSIONS: raise ValueError("只接受 BMP、JPG、JPEG、PNG 静态图片")
        target = WALLPAPER_DIR / f"{uuid.uuid4().hex}{source.suffix.lower()}"; shutil.copy2(source, target)
        self.items.append(Wallpaper(uuid.uuid4().hex, str(target), source.stem, source=source_name))

    def import_yuanqi_cache_roots(self, roots: list[Path]) -> None:
        self.cache_status.configure(text="正在扫描并复制，请稍候……")
        self.status.set("正在迁移本机元气静态缓存……")
        existing_paths = [Path(item.path) for item in self.items]

        def work() -> None:
            try:
                candidates = discover_yuanqi_cache_images(roots)
                imported = copy_yuanqi_cache_images(candidates, existing_paths)
                self._safe_after(lambda: finish(candidates, imported, None))
            except (OSError, ValueError) as exc:
                self._safe_after(lambda: finish([], [], exc))

        def finish(candidates: list[Path], imported: list[Wallpaper], error: Exception | None) -> None:
            if error:
                self.cache_status.configure(text="迁移失败")
                messagebox.showerror("迁移失败", str(error))
                return
            self.items.extend(imported)
            if imported:
                self.selected_id = imported[0].id
            self.refresh()
            self.cache_status.configure(text=f"发现 {len(candidates)} 张有效静态图，本次新增 {len(imported)} 张")
            self.status.set(f"已把 {len(imported)} 张元气静态缓存独立复制到本地资料库。")

        threading.Thread(target=work, daemon=True).start()

    def import_detected_yuanqi_cache(self) -> None:
        self.import_yuanqi_cache_roots(list(DEFAULT_YUANQI_CACHE_DIRS))

    def choose_yuanqi_cache_directory(self) -> None:
        selected = filedialog.askdirectory(title="选择元气静态壁纸缓存目录")
        if selected:
            self.import_yuanqi_cache_roots([Path(selected)])

    def import_images(self) -> None:
        paths = filedialog.askopenfilenames(title="选择静态壁纸", filetypes=[("图片", "*.bmp *.jpg *.jpeg *.png")])
        if not paths: return
        try:
            for raw in paths: self.add_file(Path(raw))
            self.selected_id = self.items[-1].id; self.refresh(); self.status.set(f"已导入 {len(paths)} 张图片。")
        except (OSError, ValueError) as exc: messagebox.showerror("导入失败", str(exc))

    def sync_official_featured(self) -> None:
        existing = {(item.title, item.source) for item in self.items}; added = 0
        try:
            for title, url in OFFICIAL_FEATURED:
                if (title, OFFICIAL_FEATURED_SOURCE) not in existing:
                    self.items.append(download_static_image(url, title, OFFICIAL_FEATURED_SOURCE)); added += 1
            if self.items: self.selected_id = self.items[-1].id
            self.refresh(); self.status.set("5 张官网公开样图已是最新。" if not added else f"已同步 {added} 张官网公开样图；这不代表账号资源已接入。")
        except (OSError, ValueError) as exc: messagebox.showerror("同步失败", str(exc))

    def apply_selected(self) -> None:
        item = self.selected()
        if not item:
            return
        item_id, item_title, item_path = item.id, item.title, Path(item.path)
        self.apply_button.configure(state="disabled", text="正在应用……")
        self.status.set(f"正在交给 Windows 应用：{item_title}")

        def work() -> None:
            try:
                set_native_wallpaper(item_path)
                self._safe_after(lambda: finish(None))
            except (OSError, ValueError) as exc:
                self._safe_after(lambda: finish(exc))

        def finish(error: Exception | None) -> None:
            self.apply_button.configure(state="normal", text="应用为壁纸")
            if error:
                self.status.set(f"Windows 未确认壁纸变更：{item_title}")
                messagebox.showerror("应用失败", str(error))
                return
            self.config["last_applied"] = item_id
            self.persist()
            self.status.set(f"已由 Windows 注册表复核应用成功：{item_title}")

        threading.Thread(target=work, daemon=True).start()

    def toggle_selected_favorite(self) -> None:
        item = self.selected()
        if item: item.favorite = not item.favorite; self.refresh()

    def open_selected_folder(self) -> None:
        item = self.selected()
        if item: os.startfile(Path(item.path).parent)

    def download_url(self) -> None:
        dialog = tk.Toplevel(self.root); dialog.title("下载已获授权的图片直链"); dialog.configure(bg=PANEL); dialog.geometry("560x190")
        tk.Label(dialog, text="粘贴通过官方网页或会员账户取得的静态图片直链", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", padx=22, pady=(22, 6))
        value = tk.StringVar(); tk.Entry(dialog, textvariable=value, width=70, relief="solid", bd=1).pack(padx=22, pady=6)
        def run() -> None:
            try:
                url = value.get().strip(); self.items.append(download_static_image(url, Path(urllib.parse.urlparse(url).path).stem or "下载壁纸", "授权直链"))
                self.selected_id = self.items[-1].id; self.refresh(); self.status.set("下载完成，已加入图库。"); dialog.destroy()
            except (OSError, ValueError) as exc: messagebox.showerror("下载失败", str(exc), parent=dialog)
        self.button(dialog, "下载并加入图库", run, accent=True).pack(pady=14)

    def open_official_site(self) -> None:
        import webbrowser; webbrowser.open(OFFICIAL_SHOWCASE_URL)

    def open_official_catalog(self, url: str = OFFICIAL_CATALOG_URL) -> None:
        import webbrowser
        webbrowser.open(url)
        self.status.set("已用浏览器打开元气官网；这不代表本客户端登录或资源接入。")

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self.root); dialog.title("定时切换"); dialog.configure(bg=PANEL); dialog.geometry("360x190")
        tk.Label(dialog, text="自动切换间隔（分钟，0 为关闭）", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(26, 7))
        value = tk.StringVar(value=str(self.config.get("interval_minutes", 0))); tk.Entry(dialog, textvariable=value, width=14, justify="center").pack()
        def save() -> None:
            try:
                minutes = int(value.get()); assert minutes >= 0
                self.config["interval_minutes"] = minutes; self.persist(); self.schedule_next(); dialog.destroy()
                self.status.set("已关闭自动切换。" if not minutes else f"将每 {minutes} 分钟切换收藏壁纸。")
            except (ValueError, AssertionError): messagebox.showerror("输入错误", "请输入 0 或正整数。", parent=dialog)
        self.button(dialog, "保存设置", save, accent=True).pack(pady=16)

    def schedule_next(self) -> None:
        if self.timer: self.timer.cancel()
        favorites = [item for item in self.items if item.favorite]; minutes = int(self.config.get("interval_minutes", 0))
        if minutes and favorites:
            self.timer = threading.Timer(minutes * 60, self.apply_next); self.timer.daemon = True; self.timer.start()

    def apply_next(self) -> None:
        favorites = [item for item in self.items if item.favorite]
        if favorites:
            last = self.config.get("last_applied"); index = next((i for i, item in enumerate(favorites) if item.id == last), -1)
            item = favorites[(index + 1) % len(favorites)]
            try: set_native_wallpaper(Path(item.path)); self.config["last_applied"] = item.id; self.persist()
            except OSError: pass
        self.schedule_next()


if __name__ == "__main__":
    instance = SingleInstance()
    if instance.already_running:
        from product import notify_already_running
        notify_already_running()
        raise SystemExit(0)
    root = tk.Tk()
    App(root)
    root.mainloop()
    instance.release()
