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

## `kvipgui` 登录页桥与关闭条件

对 `kvipgui.exe` 的命令分发表和处理函数继续做离线反汇编，确认统一登录页还有第二层
CEF 到原生桥。以下地址均为 PE 首选 VA：

- `refreshUserInfo` 在 `0x42a1da` 分发到 `0x41cc00`；处理器调用会员 SDK 包装对象，
  再以字段 `result` 把结果返回页面；
- `userLogin` 在 `0x42a311` 分发到 `0x41f460`；处理器读取 `token`、`open_id`、
  `type` 等页面参数，调用内部登录接口，再返回 `result`；
- `isUserLogin` 在 `0x42a44a` 分发到 `0x4200d0`；处理器查询统一账号状态、构造
  `result`，并在一个窗口关闭分支执行 `PostMessageW(hwnd, WM_CLOSE, 0, 0)`。

这组控制流说明登录页会把认证结果交给官方会员 SDK，并由登录窗自行关闭；没有证据表明
它通过一个可供第三方复用的窗口消息把账号凭据发给 `kwallpaperui.exe`。主宿主随后看到
账号态，更符合共享的官方 SDK 状态/监听器刷新。进程退出和窗口关闭都只是不含秘密的
完成信号，不能单独证明认证成功。

`userLogin` 是秘密承载边界，而不是公开互操作接口。本项目只记录它的字段结构以划分边界，
不调用、模拟或重放该入口，也不把 `token`/`open_id` 的运行时值写入日志或测试。代码中的
`KvipGuiLoginBridgeBoundary` 固定这一限制；可复刻部分仍是“拉起官方认证 → 等待本人操作 →
官方回调 → 重新取得用户/设备信息 → 服务端校验”。

Win32 消息导入也完成了归因：`RegisterWindowMessageW` 位于通用
`broadcastAction` 命令处理器，`isUserLogin` 使用的 `PostMessageW` 是关闭自身窗口，均不能
当作登录回调 ABI。

## 主宿主的登录后回流 ABI

继续沿 `kwallpaperui.exe` 的 `OpenVipCommand::RunVip` 反向核对，确认启动路径最终调用
`ShellExecuteW`，只返回是否成功拉起 `kvipgui.exe`；该控制流不保存子进程句柄，也不调用
`WaitForSingleObject`。因此主宿主不是靠等待登录子进程结束来认定登录成功。

主宿主内存在两套 SDK 通知基础设施：

- `KVipNotifyClient` 创建消息窗口，并注册两个 GUID 命名的 Windows 消息；其中一个无载荷
  通知直接分发回调，另一个只通过 Global Atom 临时转交两个字符串后立即删除 Atom；
- `KGalleryDlg` 明确实现 `vip_sdk::IUserInfoChangeListener`。RTTI 显示监听器子对象位于
  `KGalleryDlg + 1232`，虚表为 `0x717d24`，唯一回调为 `0x4e91d8`；
- SDK core 虚表 `+108/+112` 由包装函数 `0x4609e2/0x460a08` 成对用于监听器注册和注销；
  `KGalleryDlg` 初始化/清理路径会把自己的监听器子对象传入；
- 回调 `0x4e91d8` 引用日志 `KGalleryDlg::OnUserInfoChange Start.`，随后刷新主界面多个账号
  相关组件。

由此可把微信回流顺序修正为：“统一登录页把结果交给官方会员 SDK → SDK 发布用户信息
变更 → `KGalleryDlg::OnUserInfoChange` 刷新主界面”。`kvipgui.exe` 退出只是同一流程中的
窗口生命周期结果，不是登录凭据或成功证明。自研实现可以复刻监听器/状态机形状，但没有
官方 core 接口、产品身份和服务端校验权时，不能伪造该通知为登录成功。

状态机因此新增 `OFFICIAL_REFRESH_OBSERVED`：它只表示收到用户信息监听器信号，
`has_session` 仍为假；后续必须收到登录回调并完成用户、设备和服务端会话校验，才进入
`AUTHENTICATED`。

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

WPR Network 同期追踪显示目标进程的外连进入本机代理。后续用 TCP 所有者核对确认
`127.0.0.1:33331` 的监听进程是 `clash-verge`，且同时服务 Chrome、ChatGPT 等多个普通
应用，所以它也是代理端口，不是元气桌面 IPC；此前的 IPC 推断作废。ETL 丢失约 104 万
事件，代理也隐藏了真实远端终点，因此这次 ETW 结果只支持“经本机代理”的结论，不用于
推断微信认证域名或接口。原始 ETL 已在提取非秘密结论后删除，未进入 Git。

## 后续版本验证方案

1. 在用户本人退出前启动 WPR Network/GeneralProfile 文件模式，只记录进程、模块、目标
   主机/IP、端口和时间序列；
2. 如需再次验证新版本，只对 CEF `showlogin` 和原生统一登录启动器使用低频硬件断点；
3. 用户本人打开微信登录、扫码并确认；调试器不点击认证 UI，不读取二维码或秘密值；
4. 立即停止 ETW、分离调试器，只保留不含账号和令牌值的调用序列与主机名；含敏感值的
   原始调试/ETL 材料不进入 Git。
