"""原生壁纸管理器：图库式浏览，且只使用 Windows 原生壁纸接口。"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import threading
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageOps, ImageTk

APP_DIR = Path.home() / "AppData" / "Local" / "NativeWallpaperManager"
WALLPAPER_DIR = APP_DIR / "wallpapers"
CONFIG_PATH = APP_DIR / "config.json"
IMAGE_EXTENSIONS = {".bmp", ".jpg", ".jpeg", ".png"}
OFFICIAL_FEATURED = (
    ("樱花树下的少女", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm7.png"),
    ("日落", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm8.png"),
    ("云朵与少女", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm5.png"),
    ("无缝风铃蝴蝶房屋独寂", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm4.png"),
    ("中国故宫少女", "https://wallpaper-static.cheetahfun.com/wallpaper/sites/dynamics/vm6.png"),
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
        tk.Label(header, text="原生静态壁纸", bg=PANEL, fg=PINK, font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        controls = tk.Frame(header, bg=PANEL); controls.pack(side="right", padx=24)
        self.button(controls, "导入图片", self.import_images).pack(side="left", padx=4)
        self.button(controls, "同步精选", self.sync_official_featured).pack(side="left", padx=4)
        self.button(controls, "下载直链", self.download_url).pack(side="left", padx=4)
        self.button(controls, "定时切换", self.open_settings).pack(side="left", padx=4)

        body = tk.Frame(self.root, bg=BG); body.pack(fill="both", expand=True, padx=22, pady=18)
        sidebar = tk.Frame(body, bg=PANEL, width=180); sidebar.pack(side="left", fill="y", padx=(0, 18)); sidebar.pack_propagate(False)
        tk.Label(sidebar, text="我的资料库", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=18, pady=(22, 10))
        self.all_button = self.button(sidebar, "全部壁纸", self.show_all, width=13); self.all_button.pack(padx=16, pady=3)
        self.favorite_button = self.button(sidebar, "我的收藏", self.show_favorites, width=13); self.favorite_button.pack(padx=16, pady=3)
        tk.Frame(sidebar, bg=LINE, height=1).pack(fill="x", padx=18, pady=20)
        tk.Label(sidebar, text="资源来源", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=18)
        tk.Label(sidebar, text="· 本地导入\n· 官网公开精选\n· 已授权图片直链", justify="left", bg=PANEL, fg=TEXT,
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
        self.gallery_canvas = tk.Canvas(main, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(main, orient="vertical", command=self.gallery_canvas.yview)
        self.gallery_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y"); self.gallery_canvas.pack(fill="both", expand=True)
        self.gallery = tk.Frame(self.gallery_canvas, bg=BG)
        self.gallery_window = self.gallery_canvas.create_window((0, 0), window=self.gallery, anchor="nw")
        self.gallery.bind("<Configure>", lambda _event: self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all")))
        self.gallery_canvas.bind("<Configure>", lambda event: self.gallery_canvas.itemconfigure(self.gallery_window, width=event.width))
        self.root.bind_all("<MouseWheel>", lambda event: self.gallery_canvas.yview_scroll(int(-event.delta / 120), "units"))
        tk.Label(self.root, textvariable=self.status, bg="#eef0f5", fg="#53607a", anchor="w", padx=24, pady=10,
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", side="bottom")

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
                if (title, "元气官网公开精选") not in existing:
                    self.items.append(download_static_image(url, title, "元气官网公开精选")); added += 1
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
        import webbrowser; webbrowser.open("https://www.yuanqidesk.com/")

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
