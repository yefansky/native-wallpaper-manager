"""原生壁纸管理器：图库式浏览，且只使用 Windows 原生壁纸接口。"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageOps, ImageTk

from catalog import YuanqiOfficialApiSource

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
BG, PANEL, TEXT, MUTED, GOLD, PINK, LINE = "#f5f6fa", "#ffffff", "#1e2233", "#81879a", "#f4b734", "#fa6d9b", "#e9ebf1"


@dataclass
class Wallpaper:
    id: str
    path: str
    title: str
    favorite: bool = False
    source: str = "本地导入"


def load_config() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        return {"items": [], "interval_minutes": 0, "last_applied": ""}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": [], "interval_minutes": 0, "last_applied": ""}


def save_config(config: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def set_native_wallpaper(path: Path) -> None:
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("Windows 原生壁纸仅支持 BMP/JPG/PNG 图片")
    if not ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, str(path), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    ):
        raise ctypes.WinError()


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
        self.yuanqi_source = YuanqiOfficialApiSource()
        self.items = [Wallpaper(**item) for item in self.config.get("items", []) if Path(item["path"]).exists()]
        self.filter_favorites = False
        self.selected_id = self.config.get("last_applied") or (self.items[0].id if self.items else None)
        self.timer: threading.Timer | None = None
        self.status = tk.StringVar(value="Windows 原生壁纸模式：不会覆盖桌面或隐藏图标。")
        self.photo_refs: list[ImageTk.PhotoImage] = []
        self._build()
        self.refresh()
        self.schedule_next()

    def button(self, parent, text, command, *, accent=False, width=None):
        return tk.Button(parent, text=text, command=command, width=width, relief="flat", cursor="hand2",
                         bg=GOLD if accent else "#f1f3f8", fg=TEXT, activebackground="#ffd56d" if accent else "#e4e8f0",
                         activeforeground=TEXT, font=("Microsoft YaHei UI", 10, "bold"), padx=12, pady=8, bd=0)

    def _build(self) -> None:
        header = tk.Frame(self.root, bg=PANEL, height=78)
        header.pack(fill="x"); header.pack_propagate(False)
        tk.Label(header, text="桌面画廊", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 20, "bold")).pack(side="left", padx=(28, 8))
        tk.Label(header, text="Windows 原生模式", bg=PANEL, fg=PINK, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        nav = tk.Frame(header, bg="#f1f3f7"); nav.pack(side="left", padx=36, pady=15)
        self.nav_buttons = {}
        for key, label in (("wallpaper", "本地壁纸"), ("official", "官方资源"), ("theme", "电脑主题"), ("beauty", "系统美化"), ("components", "组件"), ("mine", "我的")):
            button = tk.Button(nav, text=label, command=lambda name=key: self.show_page(name), relief="flat", cursor="hand2", bd=0,
                               bg="#f1f3f7", fg=TEXT, activebackground=GOLD, font=("Microsoft YaHei UI", 10, "bold"), padx=16, pady=10)
            button.pack(side="left", padx=2); self.nav_buttons[key] = button
        self.button(header, "定时切换", self.open_settings).pack(side="right", padx=24)

        body = tk.Frame(self.root, bg=BG)
        self.library_page = body
        sidebar = tk.Frame(body, bg=PANEL, width=180); sidebar.pack(side="left", fill="y", padx=(0, 18)); sidebar.pack_propagate(False)
        tk.Label(sidebar, text="我的资料库", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=18, pady=(22, 10))
        self.all_button = self.button(sidebar, "全部壁纸", self.show_all, width=13); self.all_button.pack(padx=16, pady=3)
        self.favorite_button = self.button(sidebar, "我的收藏", self.show_favorites, width=13); self.favorite_button.pack(padx=16, pady=3)
        tk.Frame(sidebar, bg=LINE, height=1).pack(fill="x", padx=18, pady=20)
        tk.Label(sidebar, text="资源来源", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=18)
        tk.Label(sidebar, text="· 本地导入\n· 元气官网首页公开展示\n· 已授权图片直链", justify="left", bg=PANEL, fg=TEXT,
                 font=("Microsoft YaHei UI", 9), pady=8).pack(anchor="w", padx=18)
        self.button(sidebar, "打开元气官网", self.open_official_site, width=13).pack(side="bottom", pady=18)

        main = tk.Frame(body, bg=BG); main.pack(side="left", fill="both", expand=True)
        preview = tk.Frame(main, bg=PANEL, height=280); preview.pack(fill="x"); preview.pack_propagate(False)
        self.preview_image = tk.Label(preview, bg="#d8dce6"); self.preview_image.pack(side="left", fill="both", expand=True, padx=16, pady=16)
        detail = tk.Frame(preview, bg=PANEL, width=260); detail.pack(side="right", fill="y", padx=(0, 26), pady=26); detail.pack_propagate(False)
        tk.Label(detail, text="当前预览", bg=PANEL, fg=PINK, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        self.preview_title = tk.Label(detail, text="选择一张壁纸", bg=PANEL, fg=TEXT, wraplength=230, justify="left", font=("Microsoft YaHei UI", 18, "bold"), pady=8)
        self.preview_title.pack(anchor="w")
        self.preview_source = tk.Label(detail, text="", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)); self.preview_source.pack(anchor="w")
        self.apply_button = self.button(detail, "应用为壁纸", self.apply_selected, accent=True); self.apply_button.pack(anchor="w", pady=(20, 7))
        self.favorite_action = self.button(detail, "收藏这张", self.toggle_selected_favorite); self.favorite_action.pack(anchor="w")

        gallery_header = tk.Frame(main, bg=BG); gallery_header.pack(fill="x", pady=(18, 8))
        tk.Label(gallery_header, text="壁纸图库", bg=BG, fg=TEXT, font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        self.count_label = tk.Label(gallery_header, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 9)); self.count_label.pack(side="left", padx=10)
        self.button(gallery_header, "导入", self.import_images).pack(side="right", padx=3)
        self.button(gallery_header, "直链下载", self.download_url).pack(side="right", padx=3)
        self.button(gallery_header, "同步精选", self.sync_official_featured).pack(side="right", padx=3)
        self.gallery_canvas = tk.Canvas(main, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(main, orient="vertical", command=self.gallery_canvas.yview)
        self.gallery_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y"); self.gallery_canvas.pack(fill="both", expand=True)
        self.gallery = tk.Frame(self.gallery_canvas, bg=BG)
        self.gallery_window = self.gallery_canvas.create_window((0, 0), window=self.gallery, anchor="nw")
        self.gallery.bind("<Configure>", lambda _event: self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all")))
        self.gallery_canvas.bind("<Configure>", lambda event: self.gallery_canvas.itemconfigure(self.gallery_window, width=event.width))
        self.root.bind_all("<MouseWheel>", lambda event: self.gallery_canvas.yview_scroll(int(-event.delta / 120), "units"))
        self.theme_page = self.build_theme_page()
        self.official_page = self.build_official_page()
        self.beauty_page = self.build_beauty_page()
        self.components_page = self.build_components_page()
        self.mine_page = self.build_mine_page()
        self.pages = {"wallpaper": self.library_page, "official": self.official_page, "theme": self.theme_page, "beauty": self.beauty_page, "components": self.components_page, "mine": self.mine_page}
        tk.Label(self.root, textvariable=self.status, bg="#eef0f5", fg="#53607a", anchor="w", padx=24, pady=10,
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", side="bottom")
        self.show_page("wallpaper")

    def show_page(self, name: str) -> None:
        for page in self.pages.values(): page.pack_forget()
        self.pages[name].pack(fill="both", expand=True, padx=22, pady=18)
        for key, button in self.nav_buttons.items():
            button.configure(bg=GOLD if key == name else "#f1f3f7")
        if name == "theme": self.refresh_theme_preview()
        if name == "mine": self.refresh_mine_summary()

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

    def build_official_page(self) -> tk.Frame:
        page = tk.Frame(self.root, bg=BG)
        capabilities = self.yuanqi_source.capabilities()
        self.page_header(page, "元气官方资源", "分类保持单行；资源图片、搜索、分页和详情在下方独立客户端窗口实时显示。")
        notice = tk.Frame(page, bg="#fff7df", highlightbackground="#f1d28a", highlightthickness=1, padx=14, pady=9)
        notice.pack(fill="x", pady=(0, 10))
        tk.Label(notice, text=f"正式接入状态：{capabilities.reason} 不伪造登录态或会员原图。", bg="#fff7df", fg=PINK,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")

        categories = tk.Frame(page, bg=PANEL, padx=6, pady=7)
        categories.pack(fill="x")
        for index, (label, url) in enumerate(OFFICIAL_STATIC_CATEGORIES):
            button = tk.Button(
                categories,
                text=label,
                command=lambda target=url: self.open_embedded_catalog(target),
                relief="flat",
                cursor="hand2",
                bd=0,
                bg=GOLD if index == 0 else PANEL,
                fg=TEXT,
                activebackground="#ffd56d",
                font=("Microsoft YaHei UI", 9, "bold"),
                padx=5,
                pady=8,
            )
            button.pack(side="left", fill="x", expand=True, padx=1)

        preview = tk.Frame(page, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=28, pady=28)
        preview.pack(fill="both", expand=True, pady=(12, 0))
        tk.Label(preview, text="实时官方图库", bg=PANEL, fg=TEXT, font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        tk.Label(preview, text="点击上方任一分类，或打开完整图库。新窗口直接显示元气官方页面，资源会随官方持续更新。",
                 bg=PANEL, fg=MUTED, justify="left", wraplength=760, font=("Microsoft YaHei UI", 11), pady=12).pack(anchor="w")
        tk.Label(preview, text="客户端本体不依赖元气桌面安装；当前公开网页仍未提供会员账号授权和高清原图 API。",
                 bg=PANEL, fg=PINK, justify="left", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        actions = tk.Frame(preview, bg=PANEL); actions.pack(anchor="w", pady=(28, 0))
        self.button(actions, "在客户端内打开完整图库", lambda: self.open_embedded_catalog(OFFICIAL_CATALOG_URL), accent=True).pack(side="left", padx=(0, 8))
        self.button(actions, "用系统浏览器打开", lambda: self.open_official_catalog(OFFICIAL_CATALOG_URL)).pack(side="left")
        return page

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
        page = tk.Frame(self.root, bg=BG); self.page_header(page, "我的", "本地资料库、收藏和 Windows 主题文件。")
        self.mine_summary = tk.Label(page, text="", bg=PANEL, fg=TEXT, justify="left", anchor="nw", padx=24, pady=24, font=("Microsoft YaHei UI", 12))
        self.mine_summary.pack(fill="x")
        return page

    def visible_items(self) -> list[Wallpaper]:
        return [item for item in self.items if item.favorite or not self.filter_favorites]

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
        items = self.visible_items()
        if self.selected_id not in {item.id for item in self.items}:
            self.selected_id = items[0].id if items else None
        self.photo_refs.clear()
        for child in self.gallery.winfo_children(): child.destroy()
        for index, item in enumerate(items):
            selected = item.id == self.selected_id
            card = tk.Frame(self.gallery, bg=PANEL, highlightbackground=GOLD if selected else LINE,
                            highlightthickness=2 if selected else 1, cursor="hand2")
            card.grid(row=index // 3, column=index % 3, padx=7, pady=7, sticky="nsew")
            self.gallery.grid_columnconfigure(index % 3, weight=1)
            photo = self.image_for(Path(item.path), (220, 128)); self.photo_refs.append(photo)
            picture = tk.Label(card, image=photo, bg=PANEL, cursor="hand2"); picture.pack(fill="x", padx=8, pady=(8, 6))
            title = tk.Label(card, text=item.title, bg=PANEL, fg=TEXT, anchor="w", cursor="hand2", font=("Microsoft YaHei UI", 10, "bold"))
            title.pack(fill="x", padx=9)
            meta = tk.Label(card, text=("★ 收藏 · " if item.favorite else "") + item.source, bg=PANEL, fg=MUTED, anchor="w", cursor="hand2", font=("Microsoft YaHei UI", 8), pady=7)
            meta.pack(fill="x", padx=9)
            for widget in (card, picture, title, meta): widget.bind("<Button-1>", lambda _event, current=item: self.select(current))
        self.count_label.configure(text=f"{len(items)} 张静态壁纸")
        item = self.selected()
        if item:
            photo = self.image_for(Path(item.path), (470, 248)); self.photo_refs.append(photo)
            self.preview_image.configure(image=photo)
            self.preview_title.configure(text=item.title)
            self.preview_source.configure(text=item.source)
            self.favorite_action.configure(text="取消收藏" if item.favorite else "收藏这张")
            self.apply_button.configure(state="normal")
        else:
            self.preview_image.configure(image="")
            self.preview_title.configure(text="还没有壁纸")
            self.preview_source.configure(text="导入图片或同步官网精选开始浏览")
            self.apply_button.configure(state="disabled")
        self.all_button.configure(bg="#fff3cf" if not self.filter_favorites else "#f1f3f8")
        self.favorite_button.configure(bg="#fff3cf" if self.filter_favorites else "#f1f3f8")
        self.persist()

    def show_all(self) -> None:
        self.filter_favorites = False; self.refresh()

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
        self.mine_summary.configure(text=f"本地资料库\n\n{len(self.items)} 张静态壁纸\n{favorites} 张收藏\n\n主题文件\n{self.theme_path()}\n\n资源全部存放在本机，不保存元气账号 Cookie 或令牌。")

    def show_favorites(self) -> None:
        self.filter_favorites = True; self.refresh()

    def add_file(self, source: Path, source_name: str = "本地导入") -> None:
        if source.suffix.lower() not in IMAGE_EXTENSIONS: raise ValueError("只接受 BMP、JPG、JPEG、PNG 静态图片")
        target = WALLPAPER_DIR / f"{uuid.uuid4().hex}{source.suffix.lower()}"; shutil.copy2(source, target)
        self.items.append(Wallpaper(uuid.uuid4().hex, str(target), source.stem, source=source_name))

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
            self.refresh(); self.status.set("官网精选已是最新。" if not added else f"已同步 {added} 张官网公开精选。")
        except (OSError, ValueError) as exc: messagebox.showerror("同步失败", str(exc))

    def apply_selected(self) -> None:
        item = self.selected()
        if not item: return
        try:
            set_native_wallpaper(Path(item.path)); self.config["last_applied"] = item.id; self.persist()
            self.status.set(f"已用 Windows 原生机制应用：{item.title}")
        except (OSError, ValueError) as exc: messagebox.showerror("应用失败", str(exc))

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
        self.status.set("已打开元气官方实时资源页；浏览和登录均由官方页面处理。")

    def open_embedded_catalog(self, url: str = OFFICIAL_CATALOG_URL) -> None:
        helper = Path(__file__).with_name("official_catalog_view.py")
        try:
            subprocess.Popen(
                [sys.executable, str(helper), url],
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.status.set("已在客户端窗口中打开元气官方实时资源。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法打开官方资源", str(exc))

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
    root = tk.Tk(); App(root); root.mainloop()
