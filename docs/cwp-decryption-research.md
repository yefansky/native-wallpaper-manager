# 元气 CWP 场景包与高清源视频解密研究

更新日期：2026-08-16

## 背景与问题

元气桌面的 **scene 场景主题**（带时钟/天气等 HUD）的动态壁纸，其 `preview_video`
（预览视频）是**低清且已把时间/天气组件烘焙进每一帧**的。直接用 `preview_video`
转静态 JPEG，会把"不会更新的假时钟"固化进画面。

官方客户端实际播放的是 **CWP 场景包**（私有格式，第 2/3 版），播放器在干净背景
之上**实时叠加**时钟/天气组件。因此干净高清背景只存在于 CWP 或完整源视频里，
而这两者都被保护。

本研究的结论：**完整高清源视频（`video`/`video_4k` 字段）是"重复密钥 XOR 混淆"
的 MP4，密钥可从本机官方二进制中恢复，解密后即可拿到干净高清背景。**

---

## 一句话结论

```
video / video_4k URL 下载到的字节 = MP4 明文 XOR 循环密钥
密钥(18字节) = b"djk8&$df`223jsjh"
```

用该 18 字节密钥对下载字节做逐字节异或（`byte ^ key[i % 18]`）即得标准 MP4。

---

## 解析过程（可复现）

### 1. CWP 是加密容器

抽查本机 `%APPDATA%\kdesk\data\wallpaper\dynamic\` 下 27 个 `.cwp` 文件：

- 头部 16-20 字节结构：
  ```
  offset 0-3   : "CWP\x02" 或 "CWP\x03"（版本）
  offset 4-7   : 文件总大小（小端 uint32，与文件长度一致）
  offset 8-11  : 一个偏移/计数（v2/v3 各异）
  offset 12-15 : 版本/计数
  offset 16+   : 加密载荷
  ```
- 载荷在任意偏移**熵都是 8.0/8.0**（完全随机），无 `ftyp`/JPEG/PNG/ZIP 等任何
  可识别容器。结论：CWP 是真加密，不是简单头部混淆。

### 2. `video`/`video_4k` URL 也返回加密字节

资源服务列表返回的动态条目包含：
- `preview_video`：**未加密**的标准 MP4（`ftyp isom` 开头），但低清 + HUD 烘焙。
- `video`/`video_1920`/`video_2k`/`video_4k`：**完整高清源**，`Content-Type` 声称
  `video/mp4`，但下载后首字节不是 MP4 头，在 0/64K/1M/4M/8M/16M 各偏移熵全为 8.0。

### 3. 定位解密代码：`cfwpen.dll` / `cfwthmim.dll`

对官方二进制做字符串扫描，`cfwpen.dll` 与 `cfwthmim.dll` 含：

```
aes_encrypt_n / aes_decrypt_n
.?AVCBC_Decryption@Botan@@   .?AVCTS_Decryption@Botan@@   （Botan 加密库）
Reading encrypted file. / Decryption support not enabled.
```

反汇编 `aes_decrypt_n` 的调用者确认：函数接收 (数据, 长度, 密钥, 密钥长度, IV)，
比较 4/6/8（= AES-128/192/256）。即**密钥作为参数传入**，不是硬编码在该函数里。

### 4. 在 rdata 里找到密钥流

在 `cfwpen.dll` 的 `.rdata`（只读数据段）`0x102dec7c` 处有一个字符串：

```
djk8&$df`223jsjh
```

把它与加密视频前 18 字节逐字节 XOR，恰好得到标准 MP4 ftyp box：

```
密文(前18)        64 6a 6b 18 40 50 1d 16 09 41 5d 5e 6a 73 68 68
密钥             d j k  8  &  $  d  f  `  2  2  3  j  s  j  h
XOR → 明文       00 00 00 20 66 74 79 70 69 73 6f 6d 00 00 02 00
                  \x00\x00\x00  f t y p  i s o m ...
```

（`00 00 00 20` = MP4 box 长度 32，`66 74 79 70` = "ftyp"，`69 73 6f 6d` = "isom"）

### 5. 验证整段解密

用该 18 字节密钥循环 XOR 解整个 `video`（40MB），ffprobe 确认：

```
codec_name=h264   width=3840  height=2160   duration=10.02s   bit_rate=32Mbps
```

即**解密后是 4K H.264 + AAC 的标准 MP4**。抽取一帧为干净背景（无时钟 HUD）。

---

## 核心算法

```python
KEY = b"djk8&$df`223jsjh"  # 18 字节，来自 cfwpen.dll .rdata

def decrypt_video_bytes(data: bytes) -> bytes:
    return bytes(byte ^ KEY[i % len(KEY)] for i, byte in enumerate(data))
```

对 `video`/`video_1920`/`video_2k`/`video_4k` URL 下载的原始字节逐字节 XOR 即可。

> 说明：密钥是重复的 18 字节串，不是一次一密；它出现在官方二进制 .rdata，
> 属于"固定混淆密钥"，不是会话/设备相关的秘密。CWP 场景包本身还多一层封装
> （解出来不是直接 MP4），但 `video` URL 已直接给出干净高清源，无需解析 CWP。

---

## 如何快速上手

### 在代码中

1. 动态条目解析（`app/catalog.py`）：`source_video_url` 取
   `video_4k > video_2k > video_1920 > video`。
2. 转换（`app/dynamic_converter.py`）：`convert_preview_video_to_jpeg(url, decrypt=True)`
   下载后先 `decrypt_video_bytes` 再交给 ffmpeg 抽帧。
3. 动态 `download_original` 优先用 `source_video_url`（decrypt=True）；
   没有源视频时才回退 `preview_video`（decrypt=False）。

### 命令行快速验证

```python
KEY = b"djk8&$df`223jsjh"
data = requests.get(video_url, timeout=120).content      # 完整下载
open("clean.mp4", "wb").write(
    bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(data))
)
# 然后 ffmpeg 抽帧：
#   ffmpeg -ss 5 -i clean.mp4 -frames:v 1 frame.png
```

### 验证解密是否正确

解密后文件应以 `00 00 00 20 66 74 79 70`（ftyp isom）开头；用
`ffprobe -v error -show_entries stream=width,height` 应得到真实 4K 分辨率。

---

## 实现位置

- `app/dynamic_converter.py`：`CWP_VIDEO_XOR_KEY`、`decrypt_video_bytes()`、
  `_download_preview(decrypt=...)`、`convert_preview_video_to_jpeg(decrypt=...)`。
- `app/catalog.py`：`CatalogItem.source_video_url`；
  动态 `_parse_item` 填充；`YuanqiDynamicResourceSource.download_original`
  优先解密源视频。
- `app/native_wallpaper.py`：scene 主题转换时若存在干净源视频，
  改为"确认后使用干净源视频"，不再默认取消。

---

## CWP3 解包：算法已定位，密钥仍待确认

并行逆向结论（cfwpen.dll = cmwallpaperengine，Botan 2.13 + 自定义 AES）：

- CWP 是 `"CWP\x02/03"` 16 字节头 + **全加密体**（清单、块头、数据全部加密，熵 8.0）。
- 解密：`0x1025ca60`（crypto 构建器）→ `0x10272640`（PBKDF2-like，**1000 迭代**，salt 长 = `mode*4+4`，输出 = `mode*16+0x12`）→ `0x10271480`（AES key schedule，keylen = `mode*8+8`，mode1=AES-128/mode2=AES-192）→ `0x1025ca00`（逐 32KB AES 解密）。
- 源密钥（KDF password）是引擎对象成员（`[obj-0x2c]`），**运行时由 loader 写入**，cfwpen.dll 内无静态立即数。最强候选口令：`"&g3P8&uC"`（cfwthmim.dll 显式用它作 CWP 资源解密口令）、`"123"`、`"519000"`。但用这些口令 + PBKDF2-HMAC-SHA1 + 头部 salt 测试，**均未能解出 CWP 体**——说明 KDF 的具体哈希/盐/迭代细节与标准 PBKDF2 仍有差异，或口令另有其源。
- 完整解包仍需：精确的 KDF 实现 + 源密钥确认（静态追踪 loader 写入口令，或动态在播放器启动瞬间 hook `0x10272640` dump password/salt）。

**当前代码行为**：正式源如果是 CWP 容器，不再按 XOR-MP4 解出乱码，而是明确报"CWP 场景容器尚未实现解包"；**不回退**带 HUD 的 preview_video，因此不会再生成 1536×864 假时钟图。动态转静态的干净高清源仍走已可用的 `video`/`video_4k` XOR-MP4 路径（非 scene 动态主题）。

## 边界与安全说明

- 本解密只用于**把用户本人通过正规渠道下载的壁纸**转换为 Windows 原生静态图片
  （个人格式互转），不涉及服务端访问控制、会员权限、设备绑定或会话秘密。
- 密钥是本机官方二进制中的固定混淆串，不属于账号/设备/会话秘密；代码不读取、
  不落盘官方客户端的 Cookie、令牌或会员凭据。
- 只处理 `video`/`video_4k` 等标准 MP4 混淆源；CWP3 解包仍在研究中，不绕过登录/授权。
