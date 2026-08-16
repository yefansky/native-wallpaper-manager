"""把元气标准 preview_video 安全转换为 Windows 原生静态 JPEG。

这里只处理可由常规解码器读取的预览 MP4，不接触 CWP、加密完整视频或桌面播放器。
候选帧避开片头，并以清晰度、对比度和曝光质量选择代表帧，避免直接取首帧。
"""
from __future__ import annotations

import io
import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from PIL import Image, ImageFilter, ImageStat


MAX_PREVIEW_VIDEO_BYTES = 96 * 1024 * 1024
MIN_FRAME_WIDTH = 1280
MIN_FRAME_HEIGHT = 720


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


def _bundled_winget_tool(name: str) -> str | None:
    packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if not packages.is_dir():
        return None
    matches = sorted(packages.glob(f"Gyan.FFmpeg_*/*/bin/{name}.exe"), reverse=True)
    return str(matches[0]) if matches else None


def find_ffmpeg_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg") or _bundled_winget_tool("ffmpeg")
    ffprobe = shutil.which("ffprobe") or _bundled_winget_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ConversionUnavailable("未找到 FFmpeg，无法从动态预览中可靠提取高清帧")
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


def _download_preview(url: str, path: Path, http: object) -> None:
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
                size += len(chunk)
                if size > MAX_PREVIEW_VIDEO_BYTES:
                    raise ConversionUnavailable("预览视频超过 96 MiB 安全上限")
                output.write(chunk)
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
) -> ConvertedFrame:
    """下载标准 MP4，解码五个全分辨率候选帧并返回画质最佳的 JPEG。"""
    ffmpeg, ffprobe = tool_finder()
    with tempfile.TemporaryDirectory(prefix="native-wallpaper-preview-") as temp:
        root = Path(temp)
        video = root / "preview.mp4"
        _download_preview(url, video, http)
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
        return ConvertedFrame(jpeg, best.width, best.height, timestamp, score, len(candidates))
