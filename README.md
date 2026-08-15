# 原生壁纸管理器

一个轻量 Python Windows 程序：导入或下载你已获授权的静态图片，用 Windows 原生 `SystemParametersInfoW` 设置桌面壁纸。

它不会创建桌面覆盖层、隐藏图标、接管右键菜单或播放动态壁纸。

## 功能

- 导入 BMP/JPG/JPEG/PNG，复制到本地资料库
- 图库式资源卡片、缩略图和大图预览；点选卡片即可查看与应用
- 同步元气桌面官网首页公开展示的 5 张精选图片（不需要登录）
- 收藏、过滤、应用与移出资料库
- 仅接受 HTTPS 静态图片直链的下载入口
- 打开元气桌面官网，登录和资源选择由用户在浏览器中完成
- 收藏壁纸按分钟自动轮换

## 运行

Windows 上双击 `run.bat`，或执行：

```powershell
python app/native_wallpaper.py
```

程序数据存放在 `%LOCALAPPDATA%\NativeWallpaperManager`。

## 边界

不抓取元气桌面的客户端私有接口、不复用登录 Cookie 或会员令牌、不绕过会员限制。官网目前没有可用的网页登录/二维码入口或公开资源 API；若将来平台发布官方 API，可在此基础上新增正式适配器。
