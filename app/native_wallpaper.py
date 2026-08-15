"""原生壁纸管理器：只使用 Windows SPI 设置静态壁纸。"""
from __future__ import annotations

import ctypes
import json
import shutil
import threading
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, Button, Frame, Label, Listbox, StringVar, Tk, Toplevel, filedialog, messagebox

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
    """调用 Windows 官方 SystemParametersInfo，不创建桌面覆盖层。"""
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("Windows 原生壁纸仅支持 BMP/JPG/PNG 图片")
    result = ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, str(path), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    )
    if not result:
        raise ctypes.WinError()


def download_static_image(url: str, title: str, source: str) -> Wallpaper:
    """下载一个公开或用户已授权的静态图片到本地资料库。"""
    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if parsed.scheme != "https" or suffix not in IMAGE_EXTENSIONS:
        raise ValueError("只接受明确指向 BMP/JPG/JPEG/PNG 的 HTTPS 图片直链")
    target = WALLPAPER_DIR / f"{uuid.uuid4().hex}{suffix}"
    request = urllib.request.Request(url, headers={"User-Agent": "NativeWallpaperManager/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, target.open("xb") as output:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                raise ValueError("服务器没有返回图片内容")
            shutil.copyfileobj(response, output)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return Wallpaper(uuid.uuid4().hex, str(target), title, source=source)


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("原生壁纸管理器")
        self.root.geometry("820x520")
        self.root.minsize(700, 430)
        self.config = load_config()
        self.items = [Wallpaper(**item) for item in self.config.get("items", [])]
        self.filter_favorites = False
        self.timer: threading.Timer | None = None
        self.status = StringVar(value="就绪：不会覆盖桌面，也不会隐藏图标。")
        self._build()
        self.refresh()
        self.schedule_next()

    def _build(self) -> None:
        controls = Frame(self.root, padx=12, pady=12)
        controls.pack(fill="x")
        Button(controls, text="导入图片", command=self.import_images).pack(side=LEFT, padx=(0, 8))
        Button(controls, text="同步官网精选", command=self.sync_official_featured).pack(side=LEFT, padx=8)
        Button(controls, text="下载直链", command=self.download_url).pack(side=LEFT, padx=8)
        Button(controls, text="打开元气官网", command=self.open_official_site).pack(side=LEFT, padx=8)
        Button(controls, text="设置定时", command=self.open_settings).pack(side=RIGHT, padx=(8, 0))
        Button(controls, text="只看收藏", command=self.toggle_favorites).pack(side=RIGHT, padx=8)

        body = Frame(self.root, padx=12)
        body.pack(fill=BOTH, expand=True)
        self.listbox = Listbox(body, font=("Microsoft YaHei UI", 11), activestyle="none")
        self.listbox.pack(side=LEFT, fill=BOTH, expand=True)
        actions = Frame(body, padx=12)
        actions.pack(side=RIGHT, fill="y")
        Button(actions, text="应用为壁纸", width=15, command=self.apply_selected).pack(pady=(0, 8))
        Button(actions, text="收藏 / 取消", width=15, command=self.toggle_selected_favorite).pack(pady=8)
        Button(actions, text="在资源管理器中打开", width=15, command=self.open_selected_folder).pack(pady=8)
        Button(actions, text="移出资料库", width=15, command=self.remove_selected).pack(pady=8)
        Label(self.root, textvariable=self.status, anchor="w", padx=12, pady=10, fg="#245") .pack(fill="x")

    def visible_items(self) -> list[Wallpaper]:
        return [item for item in self.items if item.favorite or not self.filter_favorites]

    def refresh(self) -> None:
        self.listbox.delete(0, END)
        for item in self.visible_items():
            mark = "★ " if item.favorite else "  "
            self.listbox.insert(END, f"{mark}{item.title}  ·  {item.source}")
        self.persist()

    def persist(self) -> None:
        self.config["items"] = [asdict(item) for item in self.items]
        save_config(self.config)

    def selected(self) -> Wallpaper | None:
        selection = self.listbox.curselection()
        if not selection:
            messagebox.showinfo("请选择壁纸", "先在列表中选一张图片。")
            return None
        return self.visible_items()[selection[0]]

    def add_file(self, source: Path, source_name: str = "本地导入") -> Wallpaper:
        if source.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError("只接受 BMP、JPG、JPEG、PNG 静态图片")
        target = WALLPAPER_DIR / f"{uuid.uuid4().hex}{source.suffix.lower()}"
        shutil.copy2(source, target)
        item = Wallpaper(uuid.uuid4().hex, str(target), source.stem, favorite=False, source=source_name)
        self.items.append(item)
        return item

    def import_images(self) -> None:
        paths = filedialog.askopenfilenames(title="选择静态壁纸", filetypes=[("图片", "*.bmp *.jpg *.jpeg *.png")])
        if not paths:
            return
        try:
            for raw in paths:
                self.add_file(Path(raw))
            self.refresh()
            self.status.set(f"已导入 {len(paths)} 张静态壁纸。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("导入失败", str(exc))

    def sync_official_featured(self) -> None:
        """同步官网首页可见的公开展示图，不登录、不使用私有接口。"""
        existing = {(item.title, item.source) for item in self.items}
        added = 0
        try:
            for title, url in OFFICIAL_FEATURED:
                if (title, "元气官网公开精选") not in existing:
                    self.items.append(download_static_image(url, title, "元气官网公开精选"))
                    added += 1
            self.refresh()
            self.status.set("官网精选已是最新。" if added == 0 else f"已同步官网公开精选 {added} 张，可直接选择应用。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("同步失败", str(exc))

    def apply_selected(self) -> None:
        item = self.selected()
        if not item:
            return
        try:
            set_native_wallpaper(Path(item.path))
            self.config["last_applied"] = item.id
            self.persist()
            self.status.set(f"已用 Windows 原生机制应用：{item.title}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("应用失败", str(exc))

    def toggle_selected_favorite(self) -> None:
        item = self.selected()
        if item:
            item.favorite = not item.favorite
            self.refresh()

    def toggle_favorites(self) -> None:
        self.filter_favorites = not self.filter_favorites
        self.refresh()
        self.status.set("正在显示收藏。" if self.filter_favorites else "正在显示全部壁纸。")

    def open_selected_folder(self) -> None:
        item = self.selected()
        if item:
            ctypes.windll.shell32.ShellExecuteW(None, "open", "/select," + item.path, None, None, 1)

    def remove_selected(self) -> None:
        item = self.selected()
        if not item:
            return
        if not messagebox.askyesno("移出资料库", f"移除“{item.title}”的本地副本？不会影响原始导入文件。"):
            return
        try:
            Path(item.path).unlink(missing_ok=True)
            self.items.remove(item)
            self.refresh()
        except OSError as exc:
            messagebox.showerror("移除失败", str(exc))

    def download_url(self) -> None:
        dialog = Toplevel(self.root)
        dialog.title("下载已获授权的图片直链")
        dialog.geometry("560x160")
        Label(dialog, text="仅粘贴你通过官方网页/会员账户取得的静态图片直链：").pack(anchor="w", padx=14, pady=(14, 4))
        value = StringVar()
        from tkinter import Entry
        Entry(dialog, textvariable=value, width=76).pack(padx=14, pady=6)
        def run() -> None:
            url = value.get().strip()
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https":
                messagebox.showerror("下载失败", "只接受 HTTPS 图片直链。", parent=dialog); return
            suffix = Path(parsed.path).suffix.lower()
            if suffix not in IMAGE_EXTENSIONS:
                messagebox.showerror("下载失败", "链接必须明确指向 BMP/JPG/JPEG/PNG 静态图片。", parent=dialog); return
            try:
                self.items.append(download_static_image(url, Path(parsed.path).stem or "下载壁纸", "授权直链"))
                self.refresh(); self.status.set("下载完成，已加入资料库。"); dialog.destroy()
            except (OSError, ValueError) as exc:
                messagebox.showerror("下载失败", str(exc), parent=dialog)
        Button(dialog, text="下载并加入资料库", command=run).pack(pady=10)

    def open_official_site(self) -> None:
        import webbrowser
        webbrowser.open("https://www.yuanqidesk.com/")
        self.status.set("已在默认浏览器打开元气桌面官网；登录与下载由你自行完成。")

    def open_settings(self) -> None:
        dialog = Toplevel(self.root); dialog.title("定时切换"); dialog.geometry("360x155")
        Label(dialog, text="自动切换间隔（分钟，0 为关闭）：").pack(pady=(18, 4))
        value = StringVar(value=str(self.config.get("interval_minutes", 0)))
        from tkinter import Entry
        Entry(dialog, textvariable=value, width=12).pack()
        def save() -> None:
            try:
                minutes = int(value.get())
                if minutes < 0: raise ValueError
                self.config["interval_minutes"] = minutes; self.persist(); self.schedule_next(); dialog.destroy()
                self.status.set("已关闭自动切换。" if minutes == 0 else f"将每 {minutes} 分钟切换收藏壁纸。")
            except ValueError: messagebox.showerror("输入错误", "请输入 0 或正整数。", parent=dialog)
        Button(dialog, text="保存", command=save).pack(pady=12)

    def schedule_next(self) -> None:
        if self.timer: self.timer.cancel()
        minutes = int(self.config.get("interval_minutes", 0))
        favorites = [item for item in self.items if item.favorite]
        if minutes > 0 and favorites:
            self.timer = threading.Timer(minutes * 60, self.apply_next)
            self.timer.daemon = True; self.timer.start()

    def apply_next(self) -> None:
        favorites = [item for item in self.items if item.favorite]
        if favorites:
            last = self.config.get("last_applied")
            index = next((i for i, item in enumerate(favorites) if item.id == last), -1)
            item = favorites[(index + 1) % len(favorites)]
            try:
                set_native_wallpaper(Path(item.path)); self.config["last_applied"] = item.id; self.persist()
                self.root.after(0, lambda: self.status.set(f"定时切换：{item.title}"))
            except OSError: pass
        self.schedule_next()


if __name__ == "__main__":
    root = Tk()
    App(root)
    root.mainloop()
