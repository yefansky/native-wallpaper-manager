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
- `kvipgui.exe`：承载统一会员登录页。微信扫码实测启动命令为
  `--open_web=login --pid=10 --vipfrom=1035`，并派生自己的 CEF 渲染进程；

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

## CEF 登录桥与原生启动器

进一步恢复字符串交叉引用后，确认主宿主存在一张 CEF 到原生的命令分发表，其中包括：

- `showlogin`：网页请求打开统一登录；
- `ongetuserinfo`、`refreshuserinfo`：读取或刷新网页可见的登录状态；
- `logoutuser`：退出当前用户；
- `logintopicpage`、`getdeviceinfo`：登录主题页和设备能力相关命令。

`showlogin` 的分发比较点位于首选 VA `0x4853e0`。原生登录启动器入口位于首选 VA
`0x4610f2`；它先调用 `KVipSdkClient` 的登录态包装方法，然后按配置构造
`kvipgui.exe login` 命令，并可附带 `--subpid`、`--fid` 等非秘密来源参数。启动器还读取
`vip_page_config` 下的 `product` 配置，说明统一登录依赖宿主产品注册信息，不是一个只需
调用 `GetInterface` 就能复用的公开 OAuth 入口。

仓库中的 `tools/wechat_login_breakpoints.cdb` 使用两个硬件执行断点，只观察低频的
`showlogin` 和原生登录启动器入口，并只输出标签与短调用栈。高频登录态/用户信息包装
断点默认不再启用，避免冻结 CEF UI。脚本刻意不输出寄存器、函数参数、内存、二维码、
Token、Cookie 或设备标识。

## 微信扫码实机追踪结果

账号本人在默认微信页扫码并在手机端确认。非秘密进程快照确认如下顺序：

1. 主 `kwallpaperui.exe` 通过统一登录启动器拉起 `kvipgui.exe`；
2. 登录宿主命令行为
   `kvipgui.exe --open_web=login --pid=10 --vipfrom=1035`；
3. `kvipgui.exe` 再创建 CEF 渲染子进程，主壁纸 UI 和其原有渲染进程继续运行；
4. 用户完成微信确认后，`kvipgui.exe` 及登录渲染子进程退出，主
   `kwallpaperui.exe` 不退出；
5. 用户确认主界面已进入登录态。

这证明微信与 QQ 都经过统一会员登录宿主，而不是官网网页登录。`pid=10` 和
`vipfrom=1035` 是本机官方版本观察到的产品来源参数，只能作为兼容性证据，不能复制为
本项目的第三方客户端身份。

WPR Network 同期追踪显示目标进程的外连先进入本机 `127.0.0.1:7897` 代理，登录宿主
另有 `127.0.0.1:33331` 的本机 IPC 连接。ETL 丢失约 104 万事件，且代理隐藏了真实远端
终点，因此这次 ETW 结果只支持“经本机代理/IPC”的结论，不用于推断微信认证域名或接口。
原始 ETL 不进入 Git。

## 下一次动态跟踪方案

1. 在用户本人退出前启动 WPR Network/GeneralProfile 文件模式，只记录进程、模块、目标
   主机/IP、端口和时间序列；
2. 如需再次验证新版本，只对 CEF `showlogin` 和原生统一登录启动器使用低频硬件断点；
3. 用户本人打开微信登录、扫码并确认；调试器不点击认证 UI，不读取二维码或秘密值；
4. 立即停止 ETW、分离调试器，只保留不含账号和令牌值的调用序列与主机名；含敏感值的
   原始调试/ETL 材料不进入 Git。
