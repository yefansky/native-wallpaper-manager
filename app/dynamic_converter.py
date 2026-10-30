"""把元气可解码视频安全转换为 Windows 原生静态 JPEG。

支持标准 preview MP4 和可还原为标准 MP4 的 XOR 视频流；真正的 CWP3 容器若无法
由常规解码器验证，会由目录层明确回退到 preview_video。候选帧避开片头，并以
清晰度、对比度和曝光质量选择代表帧，避免直接取首帧。
"""
from __future__ import annotations

import io
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from PIL import Image, ImageFilter, ImageStat


MAX_PREVIEW_VIDEO_BYTES = 96 * 1024 * 1024
MIN_FRAME_WIDTH = 1280
MIN_FRAME_HEIGHT = 720
CACHE_VERSION = 1
# 元气把完整高清源视频（`video`/`video_4k`）做重复密钥 XOR 混淆后下发，
# 仅 `preview_video` 未加密但被场景 HUD 烘焙。密钥是本机官方二进制中的
# 固定字符串，用于对用户本人已下载内容做个人格式互转；不涉及会话/设备秘密。
CWP_VIDEO_XOR_KEY = b"djk8&$df`223jsjh"
DYNAMIC_FRAME_CACHE_DIR = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
) / "NativeWallpaperManager" / "dynamic_frame_cache"


def decrypt_video_bytes(data: bytes, offset: int = 0) -> bytes:
    """对元气加密源视频字节做重复密钥 XOR 解密，返回标准 MP4。"""
    if not data:
        return data
    key = CWP_VIDEO_XOR_KEY
    return bytes(byte ^ key[(offset + index) % len(key)] for index, byte in enumerate(data))

_cache_locks_guard = threading.Lock()
_cache_locks: dict[str, threading.Lock] = {}


class ConversionUnavailable(RuntimeError):
    """本机没有转换器，或预览视频不能可靠转换。"""


@dataclass(frozen=True)
class ConvertedFrame:
    jpeg: bytes
    width: int
    height: int
    timestamp: float
    score: float
    candidate_count: int
    from_cache: bool = False


def cache_key(url: str) -> str:
    """URL 只用于生成稳定键，不把元气地址写进缓存元数据。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cache_paths(url: str, cache_dir: Path) -> tuple[Path, Path]:
    key = cache_key(url)
    return cache_dir / f"{key}.jpg", cache_dir / f"{key}.json"


def _cache_lock(url: str) -> threading.Lock:
    key = cache_key(url)
    with _cache_locks_guard:
        return _cache_locks.setdefault(key, threading.Lock())


def load_cached_frame(url: str, cache_dir: Path = DYNAMIC_FRAME_CACHE_DIR) -> ConvertedFrame | None:
    jpeg_path, metadata_path = _cache_paths(url, Path(cache_dir))
    try:
        jpeg = jpeg_path.read_bytes()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("version") != CACHE_VERSION or not jpeg:
            raise ValueError("cache version mismatch")
        with Image.open(io.BytesIO(jpeg)) as decoded:
            if decoded.format != "JPEG":
                raise ValueError("cached frame is not JPEG")
            decoded.load()
            width, height = decoded.size
        if width < MIN_FRAME_WIDTH or height < MIN_FRAME_HEIGHT:
            raise ValueError("cached frame is below minimum resolution")
        if (width, height) != (int(metadata["width"]), int(metadata["height"])):
            raise ValueError("cached frame dimensions mismatch")
        timestamp = float(metadata["timestamp"])
        score = float(metadata["score"])
        candidate_count = int(metadata["candidate_count"])
        if not math.isfinite(timestamp) or not math.isfinite(score) or candidate_count < 1:
            raise ValueError("cached frame metadata is invalid")
        return ConvertedFrame(jpeg, width, height, timestamp, score, candidate_count, True)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        # 损坏或旧版本缓存不能影响正常转换；下次转换会原子重建。
        try:
            jpeg_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def store_cached_frame(
    url: str,
    frame: ConvertedFrame,
    cache_dir: Path = DYNAMIC_FRAME_CACHE_DIR,
) -> None:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    jpeg_path, metadata_path = _cache_paths(url, cache_dir)
    metadata = {
        "version": CACHE_VERSION,
        "width": frame.width,
        "height": frame.height,
        "timestamp": frame.timestamp,
        "score": frame.score,
        "candidate_count": frame.candidate_count,
    }
    temp_jpeg: Path | None = None
    temp_metadata: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=cache_dir, suffix=".jpg.tmp") as output:
            output.write(frame.jpeg)
            temp_jpeg = Path(output.name)
        with tempfile.NamedTemporaryFile(
            "w", delete=False, dir=cache_dir, suffix=".json.tmp", encoding="utf-8"
        ) as output:
            json.dump(metadata, output, ensure_ascii=False, separators=(",", ":"))
            temp_metadata = Path(output.name)
        os.replace(temp_jpeg, jpeg_path)
        temp_jpeg = None
        os.replace(temp_metadata, metadata_path)
        temp_metadata = None
    finally:
        if temp_jpeg is not None:
            temp_jpeg.unlink(missing_ok=True)
        if temp_metadata is not None:
            temp_metadata.unlink(missing_ok=True)


CONVERTER_TOOL_NAMES = ("ffmpeg.exe", "ffprobe.exe")
CONVERTER_TOOL_SUBDIRS = (Path("tools") / "ffmpeg" / "bin", Path("tools") / "bin")
CONVERTER_TOOL_HINT = (
    "安装包自带的 FFmpeg 转换组件缺失，无法把动态预览转成静态画面。"
    "请重新运行桌面画廊安装包修复安装（安装包内含 tools\\ffmpeg\\bin\\ffmpeg.exe 与 ffprobe.exe）；"
    "源码运行请把这两个文件放到仓库 tools\\ffmpeg\\bin 下。"
)


def _bundled_tool_bases() -> tuple[Path, ...]:
    """程序自带转换组件的候选根目录：冻结后为安装目录/运行解包目录，源码运行为仓库根。"""
    bases: list[Path] = []
    unpacked = str(getattr(sys, "_MEIPASS", "") or "")
    if unpacked:
        bases.append(Path(unpacked))
    if getattr(sys, "frozen", False):
        try:
            bases.append(Path(sys.executable).resolve().parent)
        except OSError:
            pass
    try:
        bases.append(Path(__file__).resolve().parent.parent)
    except OSError:
        pass
    unique: list[Path] = []
    for base in bases:
        try:
            resolved = base.resolve()
        except OSError:
            continue
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _tool_search_dirs() -> tuple[Path, ...]:
    """按优先级展开自带工具目录，不读取环境变量，避免被外部安装影响。"""
    dirs: list[Path] = []
    for base in _bundled_tool_bases():
        dirs.extend(base / subdir for subdir in CONVERTER_TOOL_SUBDIRS)
        dirs.append(base)
    return tuple(dirs)


def _bundled_winget_tool(name: str) -> str | None:
    packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if not packages.is_dir():
        return None
    matches = sorted(packages.glob(f"Gyan.FFmpeg_*/*/bin/{name}.exe"), reverse=True)
    return str(matches[0]) if matches else None


def find_ffmpeg_tools() -> tuple[str, str]:
    """返回转换工具路径：安装包自带组件优先，其次系统 PATH，最后兼容 WinGet 安装。

    自带组件固定在 `tools\\ffmpeg\\bin`（安装版在 `_internal\\tools\\ffmpeg\\bin`），
    这样桌面画廊不再依赖用户机器上另行安装的 FFmpeg。
    """
    for directory in _tool_search_dirs():
        bundled = tuple(directory / name for name in CONVERTER_TOOL_NAMES)
        if all(path.is_file() for path in bundled):
            return str(bundled[0]), str(bundled[1])
    ffmpeg = shutil.which("ffmpeg") or _bundled_winget_tool("ffmpeg")
    ffprobe = shutil.which("ffprobe") or _bundled_winget_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ConversionUnavailable(CONVERTER_TOOL_HINT)
    return ffmpeg, ffprobe


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=timeout,
            creationflags=flags,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ConversionUnavailable(f"预览视频解码失败：{type(exc).__name__}") from exc


def _probe_video(path: Path, ffprobe: str) -> tuple[float, int, int]:
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        30,
    )
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
        stream = payload["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(payload.get("format", {}).get("duration") or 0)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConversionUnavailable("预览视频缺少可验证的画面信息") from exc
    if width < MIN_FRAME_WIDTH or height < MIN_FRAME_HEIGHT:
        raise ConversionUnavailable(f"预览视频只有 {width}×{height}，低于高清壁纸安全下限")
    if not math.isfinite(duration) or duration <= 0:
        raise ConversionUnavailable("预览视频时长无效")
    return duration, width, height


def candidate_timestamps(duration: float) -> tuple[float, ...]:
    """避开首尾淡入淡出，在全片五个位置取样。"""
    if duration < 1:
        return (max(0.05, duration * 0.5),)
    return tuple(round(min(duration - 0.05, duration * ratio), 3) for ratio in (0.16, 0.33, 0.50, 0.67, 0.84))


def frame_quality(image: Image.Image) -> float:
    """无第三方数值库的保守画质评分：锐度为主，惩罚黑场、白场和低对比度。"""
    gray = image.convert("L")
    inset_x = max(1, gray.width // 50)
    inset_y = max(1, gray.height // 50)
    gray = gray.crop((inset_x, inset_y, gray.width - inset_x, gray.height - inset_y))
    stat = ImageStat.Stat(gray)
    brightness = stat.mean[0]
    contrast = stat.stddev[0]
    edges = gray.filter(ImageFilter.FIND_EDGES)
    sharpness = ImageStat.Stat(edges).var[0]
    histogram = gray.histogram()
    pixels = max(1, gray.width * gray.height)
    clipped = (sum(histogram[:8]) + sum(histogram[248:])) / pixels
    exposure_penalty = abs(brightness - 128.0) * 0.30
    low_contrast_penalty = max(0.0, 32.0 - contrast) * 4.0
    return sharpness + contrast * 7.0 - exposure_penalty - low_contrast_penalty - clipped * 1800.0


def _download_preview(url: str, path: Path, http: object, *, decrypt: bool = False) -> None:
    try:
        response = http.get(url, timeout=45, stream=True)
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and not (content_type.startswith("video/") or "octet-stream" in content_type):
            raise ConversionUnavailable("预览地址没有返回视频内容")
        size = 0
        with path.open("xb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                offset = size
                size += len(chunk)
                if size > MAX_PREVIEW_VIDEO_BYTES:
                    raise ConversionUnavailable("预览视频超过 96 MiB 安全上限")
                if decrypt and offset == 0 and chunk[:4] in (b"CWP\x02", b"CWP\x03"):
                    # 正式源是 CWP 私有容器，不是 XOR 混淆的 MP4；不要解出乱码。
                    raise ConversionUnavailable("正式源是 CWP 场景容器，当前版本尚未实现 CWP3 解包")
                output.write(decrypt_video_bytes(chunk, offset) if decrypt else chunk)
        if size == 0:
            raise ConversionUnavailable("预览视频为空")
    except ConversionUnavailable:
        path.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, requests.RequestException) as exc:
        path.unlink(missing_ok=True)
        raise ConversionUnavailable(f"预览视频下载失败：{type(exc).__name__}") from exc


def convert_preview_video_to_jpeg(
    url: str,
    *,
    http: object = requests,
    tool_finder: Callable[[], tuple[str, str]] = find_ffmpeg_tools,
    cache_dir: Path | None = DYNAMIC_FRAME_CACHE_DIR,
    decrypt: bool = False,
) -> ConvertedFrame:
    """下载标准 MP4，解码五个全分辨率候选帧并返回画质最佳的 JPEG。

    `decrypt=True` 时先对下载字节做重复密钥 XOR 解密（元气加密源视频）。
    """
    resolved_cache_dir = Path(cache_dir) if cache_dir is not None else None
    lock = _cache_lock(url) if resolved_cache_dir is not None else nullcontext()
    with lock:
        if resolved_cache_dir is not None:
            cached = load_cached_frame(url, resolved_cache_dir)
            if cached is not None:
                return cached

        ffmpeg, ffprobe = tool_finder()
        with tempfile.TemporaryDirectory(prefix="native-wallpaper-preview-") as temp:
            root = Path(temp)
            video = root / "preview.mp4"
            _download_preview(url, video, http, decrypt=decrypt)
            duration, expected_width, expected_height = _probe_video(video, ffprobe)
            candidates: list[tuple[float, float, Image.Image]] = []
            for index, timestamp in enumerate(candidate_timestamps(duration), start=1):
                frame_path = root / f"candidate-{index}.png"
                _run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        f"{timestamp:.3f}",
                        "-i",
                        str(video),
                        "-map",
                        "0:v:0",
                        "-frames:v",
                        "1",
                        "-an",
                        "-compression_level",
                        "3",
                        str(frame_path),
                    ],
                    45,
                )
                try:
                    with Image.open(frame_path) as decoded:
                        image = decoded.convert("RGB")
                        image.load()
                except OSError as exc:
                    raise ConversionUnavailable("候选帧不是有效图片") from exc
                if image.size != (expected_width, expected_height):
                    raise ConversionUnavailable("解码帧尺寸与视频流元数据不一致")
                candidates.append((frame_quality(image), timestamp, image))
            if not candidates:
                raise ConversionUnavailable("没有提取到可用候选帧")
            score, timestamp, best = max(candidates, key=lambda value: value[0])
            output = io.BytesIO()
            best.save(output, format="JPEG", quality=95, subsampling=0, optimize=True)
            jpeg = output.getvalue()
            with Image.open(io.BytesIO(jpeg)) as verified:
                verified.verify()
            converted = ConvertedFrame(jpeg, best.width, best.height, timestamp, score, len(candidates))
            if resolved_cache_dir is not None:
                try:
                    store_cached_frame(url, converted, resolved_cache_dir)
                except OSError:
                    # 缓存不可写时仍返回转换成果，不让优化路径破坏核心功能。
                    pass
            return converted
