# 微信扫码登录逆向记录

更新日期：2026-08-16

## 目标与边界

目标是为自研客户端还原可验证的微信扫码状态与官方原生桥调用顺序。分析只覆盖签名
有效的本机官方程序、DLL ABI、进程/窗口关系、IPC 和网络元数据；不提取或重放现有
令牌，不绕过 TLS、验证码、扫码确认、设备绑定或会员权限，也不复制“金山毒霸”的
产品/App 身份。

## 已确认的进程和模块关系

- `kwallpaper.exe`：父进程和 `kwallpaper:` 深链入口；
- `kwallpaperui.exe` 主进程：拥有“元气壁纸”窗口，并加载 `kvipsdk.dll`；
- 第二个 `kwallpaperui.exe`：CEF 渲染进程，不加载 `kvipsdk.dll`；
- `kvipgui.exe`：QQ 入口使用的独立统一会员授权窗口；微信和手机页面当前显示在壁纸
  主宿主的快速登录弹窗中。

四个目标均为带有效金山签名的 32 位 PE。动态只读附加确认当前 `kvipsdk.dll` 版本为
`9.3.0.3297`，产品描述为 `Kingsoft Security - pure vip core`。

## `kvipsdk.dll!GetInterface` ABI

DLL 只有一个导出 `GetInterface`。反汇编显示它是清理 8 字节参数的 32 位调用：第二个
参数是输出接口指针，第一个参数是版本化接口描述符；失败返回 0。壁纸主宿主通过
`LoadLibraryW("kvipsdk.dll")`、`GetProcAddress("GetInterface")` 动态加载 SDK，然后先取
`vip_sdk_config_interface`，再由接口管理器取得 `vip_sdk_core_interface`。

调用方传入的配置接口描述符不只是接口名：名字前还有一段版本/标识数据。这与 SDK
请求头中的产品 ID、App ID、设备 ID 要求一致，不能把接口名或导出函数单独当作开放
登录 API。

## 主宿主的会员 SDK 包装层

MSVC RTTI 和虚表恢复确认 `KVipSdkClient`、`KVipNotifyClient`、`KUserInfoDlg`、
`DelayLoginTask`、`UserInfoManager` 等原生类型存在。`KVipSdkClient` 包装层通过 core
接口虚表调用登录态、刷新令牌、刷新用户信息、VIP 状态和扩展刷新方法；用户信息解析
字段包括 `open_id`、`token`、`is_logout`、`from`、`nickname`、`avatar`、会员到期时间
和会员类型。字段名用于确认结构，不读取运行时字段值。

仓库提供 `tools/msvc_rtti_vtables.py`，可在不加载目标 DLL 的情况下重复提取 32 位 MSVC
RTTI 与虚表地址。静态地址使用 PE 首选基址，动态断点需要按实际 ASLR 基址换算。

## 二维码类名纠偏

`kwallpaperui.exe` 中确实存在 `DownloadQRCodeTask`、`QueryScanTask`、
`QRCodeQueryWorker`、`QRCodeScanListener`。但进一步恢复虚表并反汇编任务函数后发现，
它们引用 `kwallpaperui_promoteqr`、`/v1/promote/qrcode`、
`/v1/promote/get_download` 和推广设备参数，属于分享/推广二维码，不是微信认证链。

因此后续微信追踪不能再以这些类名设断点。真正的微信扫码要从统一会员 core 接口、
快速登录弹窗回调、窗口消息和登录期间网络连接元数据定位。

## 下一次动态跟踪方案

1. 在用户本人退出前启动 WPR Network/GeneralProfile 文件模式，只记录进程、模块、目标
   主机/IP、端口和时间序列；
2. 对主 `kwallpaperui.exe` 附加 32 位 CDB，记录 `kvipsdk!GetInterface`、core 接口登录
   方法、窗口消息注册和回调命中；
3. 用户本人打开微信登录、扫码并确认；调试器不点击认证 UI，不读取二维码或秘密值；
4. 立即停止 ETW、分离调试器，只保留不含账号和令牌值的调用序列与主机名；含敏感值的
   原始调试/ETL 材料不进入 Git。
