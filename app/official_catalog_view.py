"""在独立原生窗口中显示元气官方完整壁纸站。

页面、搜索、分类、分页和详情均由官方站实时提供；本程序不读取 Cookie、
不注入脚本、不解析页面资源，也不拦截下载请求。
"""
from __future__ import annotations

import sys
import urllib.parse

import webview


OFFICIAL_HOST = "bizhi.cheetahfun.com"
DEFAULT_URL = f"https://{OFFICIAL_HOST}/dn/j/"


def validated_official_url(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOST:
        raise ValueError("只允许打开元气官方完整壁纸站的 HTTPS 页面")
    return raw


def main() -> None:
    requested = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    url = validated_official_url(requested)
    webview.create_window(
        "元气官方实时资源 · 桌面画廊",
        url,
        width=1280,
        height=820,
        min_size=(960, 640),
    )
    webview.start()


if __name__ == "__main__":
    main()
