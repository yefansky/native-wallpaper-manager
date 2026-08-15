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
- 登录窗由 `kwallpaperui.exe` 内嵌的原生/CEF 宿主显示，而不是官网页面。

因此本地状态机把拉起登录后的默认阶段细化为 `QR_PENDING`，并为手机和 QQ 分别保留
`PHONE_PENDING`、`QQ_PENDING`。这些阶段只描述用户可见的认证界面；只有
`callbackOnLogin` 成功后重新取得用户、设备信息并通过服务端校验，才会进入
`AUTHENTICATED`。为避免保存临时认证材料，本项目没有留存或解码本次二维码截图。

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

## PC 原生侧的模块边界

对本机已安装、带有效签名的官方二进制只做了可见字符串和导入表检查，没有反汇编、
注入或读取账号数据。当前符号能把原生侧进一步拆成三层：

- `kwallpaperui.exe`：包含 `KUserInfoDlg`、`DelayLoginTask`、
  `UserInfoManager`；负责登录窗口和登录后用户状态刷新；
- 同一模块包含 `DownloadQRCodeTask`、`QRCodeQueryWorker`、
  `QueryScanTask`、`QRCodeScanListener`；说明“使用其他账号”中的扫码是原生任务，
  不是官网里隐藏的登录表单；
- `kvipgui.exe`：包含 `GetRecentCommonLoginList`、`DelRecentLoginData`、
  `login_type`、`product_login_list` 等入口；这与实机看到的“上次登录账号 + 一键登录”
  相吻合；
- `KVipSdkClient/kvipsdk.dll`：承接登录后的统一账号/会员会话，再把结果提供给壁纸 UI。

官方运行日志中出现的登录网关主机为 `viplogin.ijinshan.com`，会员会话校验路径包含
`https://wpvip.duba.net/api/sdk/user/userInfo`。对登录网关根地址做无账号、无参数访问
会直接返回 `403 Forbidden`，说明它不是可独立打开的网页登录页。检查日志时只抽取了
主机名和无查询参数路径，没有读取或记录账号标识、令牌或请求参数值。

```text
H5 navigate2Login
        │
        ▼
kwallpaperui：登录窗 / 上次账号 / 二维码任务
        │
        ▼
kvipgui + kvipsdk：统一账号登录、设备和会员会话
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
