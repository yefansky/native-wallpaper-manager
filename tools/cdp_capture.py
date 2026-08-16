"""通过 CEF/Chromium 远程调试端口捕获登录页网络协议结构。

用途：在叶帆本人下一次微信扫码登录时，用 Chrome DevTools Protocol 观察登录页
(由 kvipgui.exe 的 CEF 承载) 发出的网络请求。只记录非秘密结构：
请求方法、URL 路径、查询参数名、请求/响应头名、JSON/表单字段名与长度、状态码；
令牌、二维码内容、账号标识和设备值一律脱敏，不落盘明文秘密。

不绕过 TLS：CDP 是浏览器自身的调试接口，观察的是应用层已解密的页面请求，
不进行中间人解密，也不提取或重放会话秘密。

用法:
    python tools/cdp_capture.py --launch "kvipgui.exe --open_web=login --pid=10 --vipfrom=1035" --out capture.jsonl
    python tools/cdp_capture.py --attach 9222 --out capture.jsonl
    python tools/cdp_capture.py --self-test --port 9333   # 用 Edge 验证 CDP 客户端逻辑
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

DEFAULT_PORT = 9222
SENSITIVE_HEADERS = {
    "cookie", "authorization", "proxy-authorization", "set-cookie",
    "x-cf-productid", "x-cf-appid", "x-cf-device-id", "token", "openid",
}
SENSITIVE_PARAM_HINTS = ("token", "ticket", "openid", "open_id", "device", "uid", "userid", "session", "code", "key", "sign", "secret", "qr")


def mask_value(value: str) -> str:
    if value is None:
        return "<null>"
    value = str(value)
    if not value:
        return "<empty>"
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:10]
    return f"<len={len(value)} sha={digest}>"


def redact_query(url: str) -> str:
    """保留路径，只记录查询参数名，值全部脱敏。"""
    url = url.split("#")[0]
    if "?" not in url:
        return url
    base, query = url.split("?", 1)
    params = []
    for pair in query.split("&"):
        if not pair:
            continue
        name = pair.split("=", 1)[0]
        params.append(f"{name}=<v>")
    return f"{base}?{'&'.join(params)}"


def summarize_body(body: str | None, content_type: str) -> dict[str, object]:
    """从请求/响应体提取字段名与长度；不记录秘密值。"""
    if not body:
        return {}
    if content_type and "application/json" in content_type:
        try:
            value = json.loads(body)
            return summarize_json(value)
        except (json.JSONDecodeError, TypeError):
            return {"body": "json-parse-error"}
    if content_type and "x-www-form-urlencoded" in content_type:
        fields = {}
        for pair in body.split("&"):
            if "=" in pair:
                name, raw = pair.split("=", 1)
                fields[name] = mask_value(raw)
            else:
                fields[pair] = "<no-value>"
        return {"form_fields": fields}
    if content_type and content_type.startswith("image/"):
        return {"body": f"binary len={len(body)}"}
    return {"body": mask_value(body)}


def summarize_json(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                result[str(key)] = summarize_json(item)
            else:
                result[str(key)] = mask_value(item)
        return result
    if isinstance(value, list):
        return {"array_len": len(value), "first": summarize_json(value[0]) if value else None}
    return mask_value(value)


class MaskedRequest:
    def __init__(self, request_id: str):
        self.id = request_id
        self.method = ""
        self.url = ""
        self.headers: list[str] = []
        self.post_field_names: list[str] = []
        self.status = None
        self.response_header_names: list[str] = []
        self.response_field_names: list[str] = []
        self.finished = False

    def as_record(self) -> dict[str, object]:
        return {
            "request_id": self.id,
            "method": self.method,
            "url": redact_query(self.url),
            "request_header_names": sorted(set(self.headers)),
            "post_field_names": sorted(set(self.post_field_names)),
            "status": self.status,
            "response_header_names": sorted(set(self.response_header_names)),
            "response_field_names": sorted(set(self.response_field_names)),
            "finished": self.finished,
        }


class CdpNetworkCapture:
    def __init__(self, port: int, output: Path, mask_headers: bool = True):
        self.port = port
        self.output = output
        self.mask_headers = mask_headers
        self._records: dict[str, MaskedRequest] = {}
        self._seq = 0

    def list_pages(self) -> list[dict[str, object]]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def attach(self) -> None:
        pages = self.list_pages()
        target_url = pages[0]["webSocketDebuggerUrl"] if pages else None
        if not target_url:
            raise RuntimeError("没有发现可调试页面")
        print(f"[cdp] attach -> {target_url}", flush=True)
        self.ws = websocket.create_connection(target_url, timeout=15, suppress_origin=True)
        self.ws.settimeout(15)
        self.send("Network.enable")
        self.send("Runtime.enable")

    def send(self, method: str, params: dict | None = None) -> None:
        msg = {"id": self._next_id(), "method": method, "params": params or {}}
        self.ws.send(json.dumps(msg))

    def _log(self, record: dict[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        print(f"[net] {record.get('method','')} {record.get('url','')}", flush=True)
        with self.output.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def run(self, duration: float | None = None) -> None:
        print(f"[cdp] listening on port {self.port}; Ctrl+C 停止。", flush=True)
        deadline = time.time() + duration if duration else None
        while True:
            if deadline and time.time() > deadline:
                break
            try:
                message = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except (websocket.WebSocketConnectionClosedException, OSError):
                print("[cdp] 连接已关闭", flush=True)
                break
            method = message.get("method", "")
            params = message.get("params", {})
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                req_id = params.get("requestId", "")
                record = self._records.setdefault(req_id, MaskedRequest(req_id))
                record.method = request.get("method", "")
                record.url = request.get("url", "")
                headers = request.get("headers", {})
                record.headers = [str(k) for k in headers.keys()]
                post_data = request.get("postData") or ""
                if post_data:
                    content_type = str(request.get("headers", {}).get("Content-Type", ""))
                    summary = summarize_body(post_data, content_type)
                    if "form_fields" in summary:
                        record.post_field_names = list(summary["form_fields"])
                    elif isinstance(summary, dict):
                        record.post_field_names = [str(k) for k in summary.keys()]
            elif method == "Network.responseReceived":
                response = params.get("response", {})
                req_id = params.get("requestId", "")
                record = self._records.setdefault(req_id, MaskedRequest(req_id))
                record.status = response.get("status")
                headers = response.get("headers", {})
                record.response_header_names = [str(k) for k in headers.keys()]
            elif method == "Network.loadingFinished":
                req_id = params.get("requestId", "")
                if req_id in self._records:
                    self._records[req_id].finished = True
                    self._log(self._records[req_id].as_record())

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def wait_for_debugger(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                pages = json.loads(resp.read().decode("utf-8"))
            if pages:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"端口 {port} 上等待远程调试页面超时")


def launch_target(command: str, port: int) -> subprocess.Popen:
    parts = command.split()
    exe = parts[0]
    args = parts[1:] + [
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
    ]
    print(f"[cdp] 启动: {exe} {' '.join(args)}", flush=True)
    return subprocess.Popen(args, cwd=os.path.dirname(exe) or None)


def self_test(port: int) -> None:
    """用本机 Edge 验证 CDP 客户端逻辑（不需要官方客户端）。"""
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    if not os.path.exists(edge):
        edge = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    proc = None
    if os.path.exists(edge):
        user_data = str(Path.home() / "AppData" / "Local" / "Temp" / "cdp_edge_profile")
        proc = subprocess.Popen([edge, f"--remote-debugging-port={port}", f"--user-data-dir={user_data}",
                                 "--remote-allow-origins=*", "https://example.com/"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for_debugger(port, timeout=30)
        capture = CdpNetworkCapture(port, Path("cdp_self_test.jsonl"))
        capture.attach()
        print("[cdp] self-test: 已连接到浏览器，CDP 客户端逻辑正常。", flush=True)
        time.sleep(3)
    finally:
        if proc:
            proc.terminate()
    print("[cdp] self-test 完成。")


def main() -> int:
    parser = argparse.ArgumentParser(description="CDP 网络协议结构捕获")
    parser.add_argument("--launch", help="要启动的目标及其参数，例如 kvipgui.exe --open_web=login ...")
    parser.add_argument("--attach", type=int, default=0, help="附加到已在运行的远程调试端口")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--out", default="cdp_capture.jsonl", help="输出 JSONL 文件")
    parser.add_argument("--duration", type=float, default=0, help="自动停止秒数，0 表示手动")
    parser.add_argument("--self-test", action="store_true", help="用 Edge 验证 CDP 客户端")
    args = parser.parse_args()

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.self_test:
        self_test(args.port)
        return 0

    proc = None
    if args.launch:
        proc = launch_target(args.launch, args.port)
    if args.attach:
        wait_for_debugger(args.attach, timeout=30)
        args.port = args.attach

    if not args.launch and not args.attach:
        parser.error("必须提供 --launch 或 --attach 之一")

    try:
        wait_for_debugger(args.port, timeout=60)
        capture = CdpNetworkCapture(args.port, output)
        capture.attach()
        capture.run(duration=args.duration or None)
        capture.close()
    finally:
        if proc:
            proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
