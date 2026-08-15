# 元气账号与资源接入研究

更新日期：2026-08-16

## 实机结论

官方 PC 客户端的一键登录已由账号本人完成并验证。登录后可见账号头像、个人页、`我点赞的 (23)`、完整静态壁纸分类和实时资源卡片，说明官方客户端已经恢复有效的服务端账号会话，而不只是显示本地保存的头像。

## 官方客户端的实际结构

官方客户端加载的 H5 页面不是一个可独立使用的网页登录客户端。公开静态 JavaScript 显示，页面依赖原生宿主注入的方法：

- `navigate2Login`：转交原生客户端发起登录；
- `callbackOnLogin`：把原生登录结果通知页面；
- `GET_USER_INFO`：由原生客户端提供用户会话上下文；
- `GET_DEVICE_INFO`：由原生客户端提供设备上下文。

```text
QQ / 微信 / 手机号
        │
        ▼
官方元气原生客户端 ── 原生桥 ──► 客户端内 H5 ──► 资源、收藏与会员服务
```

因此，直接嵌入官方 H5 或修改 User-Agent 仍会被识别为普通网页：页面缺少官方原生桥，也没有平台签发给本项目的独立客户端身份。

## 公开 sourcemap 还原出的精确调用顺序

2026-08-16 重新读取官方 H5 当前发布的 `app.be8ed11e.js.map`，其中保留了原始
`src/bridges/base.js`、`src/bridges/utils.js`、`src/bridges/constants.js` 和
`src/utils/session.js`。可确认调用顺序为：

- 当前 H5 入口：`https://yuanqi-wallpaper-h5.zhhainiao.com/`
- 当前脚本 sourcemap：`https://yuanqi-wallpaper-h5.zhhainiao.com/js/app.be8ed11e.js.map`

1. H5 检查 `window.navigate2Login.postMessage` 是否由原生宿主注入；
2. H5 先安装全局回调 `window.callbackOnLogin`，再调用 `navigate2Login.postMessage()`；
3. 原生登录完成后执行 `callbackOnLogin`；
4. H5 再分别调用 `getUserInfo/callbackUserInfo` 和
   `getDeviceInfo/callbackDeviceInfo`；
5. 会话对象由用户信息和设备信息合并而成；每次业务请求将 `uid`、会话令牌和
   `deviceId` 放进请求体的 `common` 字段；
6. 只有资源服务继续接受该会话，登录才真正成立；前端回调本身不能代表授权成功。

本项目已在 `app/yuanqi_auth.py` 中复刻上述桥方法和状态机，并以单元测试固定方法
名、回调名以及“回调后必须服务端校验”的约束。代码不会记录或持久化令牌，也没有
把官方前端中出现的产品参数当作本项目可使用的客户端凭据。

## “使用其他账号登录”的实机页面

2026-08-16 由账号本人把官方电脑版停在“元气壁纸-快速登录”窗口，本项目只观察
页面结构，没有点击、输入、扫码或解析二维码。页面可确认：

- 默认选中“微信登录”，中央显示二维码和“微信扫一扫登录”；
- 顶部并列提供“手机登录”标签；
- “其他方式登录”下方提供 QQ 入口；
- 页面同时展示《用户协议》和《隐私政策》；
- 登录入口由 `kwallpaperui.exe` 发起，实际统一登录页由 `kvipgui.exe` 及其 CEF 子进程
  承载，而不是官网页面。

因此本地状态机把拉起登录后的默认阶段细化为 `WECHAT_QR_PENDING`（保留
`QR_PENDING` 兼容别名），并为手机和 QQ 分别保留 `PHONE_PENDING`、
`QQ_QR_PENDING`。这些阶段只描述用户可见的认证界面；只有
`callbackOnLogin` 成功后重新取得用户、设备信息并通过服务端校验，才会进入
`AUTHENTICATED`。为避免保存临时认证材料，本项目没有留存或解码本次二维码截图。

### 微信扫码宿主与回流

账号本人完成了一次默认微信扫码登录。进程级只读追踪确认：

1. 主 `kwallpaperui.exe` 拉起
   `kvipgui.exe --open_web=login --pid=10 --vipfrom=1035`；
2. `kvipgui.exe` 创建独立 CEF 渲染子进程承载微信扫码页；
3. 用户在微信手机端确认后，`kvipgui.exe` 和登录渲染子进程退出；
4. 主 `kwallpaperui.exe` 继续运行并进入账号态。

本地模型据此新增 `WechatLoginSurface` 和 `WechatAuthReturnSurface`，固定“统一登录宿主
启动 → 本人扫码确认 → 外部宿主关闭 → 官方回调/用户信息刷新 → 服务端校验”的非秘密
流程。命令行中的产品来源参数只是当前官方版本的观察值，不是可供本项目复制的授权
凭据。

### 手机登录标签

由账号本人切换到“手机登录”后再次只读观察，页面显示：

1. “输入手机号”输入框；
2. “输入验证码”输入框与“获取验证码”按钮；
3. “登录”按钮；
4. “登录即同意《用户协议》《隐私政策》”提示，没有单独的同意复选框；
5. QQ 仍作为“其他方式登录”入口；底部提示微信、QQ、手机号账号独立且数据不互通。

本地模型据此增加 `PHONE_CODE_PENDING` 和 `PHONE_SUBMITTING`：用户请求验证码后等待
短信，提交手机号与验证码后等待原生 `callbackOnLogin`。手机号和验证码只允许作为一次
后端调用的瞬时参数，不能写入 `AuthSnapshot`、日志或配置。由于平台没有提供第三方
短信认证接口，当前不可用后端仍会明确拒绝请求，不发送模拟短信，也不伪造登录成功。

### QQ 委托登录窗口

由账号本人点击 QQ 入口后，登录不再停留在 `kwallpaperui.exe` 的快速登录弹窗，而是由
`kvipgui.exe` 拉起独立的“QQ帐号安全登录”窗口。只读观察确认：

- 默认方式为“快捷登录”，提示使用 QQ 手机版扫码；
- 页面提供“密码登录”“注册账号”“意见反馈”；
- 授权方显示为“金山毒霸”，申请权限为“使用你的QQ头像、昵称信息”；
- 页面声明授权即同意 QQ 服务协议和 QQ 隐私保护指引；
- 内部可访问性结构包含腾讯 `ptlogin_iframe`。

这说明 QQ 认证是统一会员 SDK 委托给腾讯登录页的独立授权流程，并非元气 H5 自己实现。
本地状态机把默认方式记为 `QQ_QR_PENDING`，切换腾讯密码页时记为
`QQ_PASSWORD_PENDING`；两者都只等待外部宿主完成，不允许自研客户端接收或保存 QQ
密码。实机显示的“金山毒霸”授权方也不能被当作本项目可复用的客户端身份。观察过程
没有保存或解析 QQ 二维码。

### QQ 授权后的宿主回流

账号本人在 QQ 手机版确认授权后，只读观察到如下顺序：

1. `kvipgui.exe` 的“QQ帐号安全登录”窗口关闭；
2. `kwallpaperui.exe` 内的“元气壁纸-快速登录”弹窗关闭；
3. 主窗口左上角原“登录”按钮被账号头像替换；
4. 静态分类前新增“关注”，实时资源内容刷新。

这组变化说明统一会员宿主把成功结果回流给壁纸 UI，并触发账号态刷新。它仍不是自研
客户端可以信任的授权凭据：UI 头像和“关注”标签都可能只是表现层状态。因此实现仍以
桥回调为入口，随后严格按 `getUserInfo`、`getDeviceInfo`、资源服务校验的顺序执行；测试
固定了该调用顺序，并验证失败回调不会因为看见宿主 UI 已刷新而进入登录态。观察过程
没有打开账号详情，也没有读取令牌、设备标识或客户端会话文件。

## 本机可发现的宿主入口是否能供第三方复用

QQ 授权回流后又做了一轮不加载、不调用的只读能力检查：

- Windows 注册了 `kwallpaper:` URL 协议，命令仅为官方 `kwallpaper.exe "%1"`；注册项
  没有第三方客户端登记、授权回调或 OAuth 元数据，因此它只能证明官方程序支持自己的
  深链入口，不能把自研程序变成登录宿主；
- `kvipsdk.dll` 的 PE 导出表只有一个 `GetInterface`；可见接口标记包含
  `vip_sdk_config_interface`、`vip_sdk_core_interface`、`vip_sdk_infoc_interface`，说明
  它是通过版本化内部接口对象工作，而不是公开的命令行登录 API；
- SDK 可见字符串包含 `X-Cf-Productid`、`X-Cf-Appid`、`X-Cf-Device-Id` 等请求头名称，
  与“产品身份 + 应用身份 + 设备身份”的原生宿主模型一致；本次没有查找、读取或记录
  这些字段的实际值；
- 安装目录中的公共配置数据不是明文接口说明。没有尝试解密、修改或把官方配置复制到
  本项目。

因此，`kwallpaper:` 深链和 `GetInterface` 导出都不构成受支持的第三方登录能力。若要让
自研客户端真正独立登录，仍需要平台为本项目签发产品/App 身份，提供 SDK 头文件或稳定
ABI、授权回调规范以及资源服务端校验权限。缺少任一项时，当前
`UnavailableYuanqiLoginBackend` 的拒绝行为是正确结果。

## PC 原生侧的模块边界

对本机已安装、带有效签名的官方二进制做了 PE、RTTI、虚表、反汇编和只读控制流追踪；
没有读取运行时账号字段、令牌、二维码或设备标识，也没有修改认证结果。当前证据能把
原生侧进一步拆成三层：

- `kwallpaperui.exe`：包含 `KUserInfoDlg`、`DelayLoginTask`、
  `UserInfoManager`；负责登录窗口和登录后用户状态刷新；
- 同一模块包含 `DownloadQRCodeTask`、`QRCodeQueryWorker`、
  `QueryScanTask`、`QRCodeScanListener`。进一步恢复虚表并反汇编后确认这些类引用的是
  `/v1/promote/qrcode` 和推广下载参数，属于分享/推广二维码，不能再作为微信登录实现
  的证据；
- `kvipgui.exe`：包含 `GetRecentCommonLoginList`、`DelRecentLoginData`、
  `login_type`、`product_login_list` 等入口；这与实机看到的“上次登录账号 + 一键登录”
  相吻合；
- `KVipSdkClient/kvipsdk.dll`：承接登录后的统一账号/会员会话，再把结果提供给壁纸 UI。

官方运行日志中出现的登录网关主机为 `viplogin.ijinshan.com`，会员会话校验路径包含
`https://wpvip.duba.net/api/sdk/user/userInfo`。对登录网关根地址做无账号、无参数访问
会直接返回 `403 Forbidden`，说明它不是可独立打开的网页登录页。检查日志时只抽取了
主机名和无查询参数路径，没有读取或记录账号标识、令牌或请求参数值。

```text
H5 navigate2Login / native showlogin
        │
        ▼
kwallpaperui：登录桥 / 统一登录启动器 / 主界面状态
        │
        ▼
kvipgui：微信 / 手机 / QQ 统一登录页
        │
        ▼
kvipsdk：统一账号、设备和会员会话
        │
        ▼
callbackOnLogin → getUserInfo + getDeviceInfo → 服务端校验
```

## 独立客户端的正式接入条件

在不依赖已安装官方客户端的前提下，元气适配器需要同时具备：

1. 平台认可的客户端身份和授权回调；
2. 用户可见、可撤销的 QQ/微信/手机号授权流程；
3. 账号、设备和会员权益的服务端校验；
4. 分类、分页、搜索、收藏、预览和静态原图下载接口；
5. 可验证的图片类型、尺寸、散列和落盘结果。

缺少这些条件时，`app/catalog.py` 的正式适配器保持不可用，不返回模拟会员数据，也不把公开预览图冒充高清原图。

## 安全边界

本研究只记录公开前端代码中的桥方法名和调用方向。项目不会提取、复制或复用官方客户端的 Cookie、令牌、设备标识或会员凭据，不绕过设备绑定、会员权限或服务端访问控制。
