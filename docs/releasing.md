# Windows 安装包和更新发布

推送到 GitHub `main` 后，`Windows release` 工作流运行测试、PyInstaller 打包和 Inno Setup 编译，再发布 GitHub Release。普通本地 commit、其他分支 push 不发布。也可在 Actions 手动运行。构建独立排队，不设置会替换待运行任务的全局并发组。

CI 版本为 `0.1.<github.run_number>`，只写构建工作区的 `app/app_version.py`，不回写源码。Tag 为 `v<version>`，发布附件：

- `DesktopGallery-<version>-Setup.exe`
- `DesktopGallery-<version>-Setup.exe.sha256`（SHA-256 十六进制与安装包文件名）

先建立草稿，附件上传完整后才公开为正式 Release。重跑已正式发布版本保留原附件；失败草稿允许重跑补齐。通过 GitHub API 的 `make_latest=legacy` 由服务器按日期和语义版本确定 Latest，避免并行工作流强制把较旧版本标成 Latest。更换工作流或版本前缀时保持版本递增，不能重用正式版本号。

安装路径固定为 `%LOCALAPPDATA%\Programs\DesktopGallery`，无需管理员权限，默认创建当前用户桌面图标和开始菜单入口。固定 AppId 支持原位升级，卸载仅删除安装内容，不清理应用的用户数据目录。安装器使用 `Local\NativeWallpaperManagerSingleInstance` 互斥锁保护运行中的应用；应用更新助手必须等待主程序完全退出后启动安装器。`/SILENT /RESTARTAPP` 安装结束自动重启应用；普通交互安装可选择立即启动。

主程序 `DesktopGallery.exe` 为无控制台 GUI；微信登录独立为 `DesktopGalleryLogin.exe` 控制台进程，供主进程隐藏窗口并捕获标准输出。两者共用 `_internal` 依赖，发布时必须包含整个 PyInstaller 目录。登录需要系统 WebView2 Runtime；动态转换仍需要原应用要求的外部 ffmpeg/ffprobe，这次打包没有附带第三方视频工具。

当前发行仓库公开，客户端匿名读取 Release 即可检查并下载更新。CI 仍使用 GitHub 自动注入的 `GITHUB_TOKEN`，仅用于发布步骤，不打入应用；禁止把开发者 token 写入代码、构建产物或 Release。仅当更新源是私有镜像或 Fork 时，才需要用户自配对该仓库具有 Contents 只读权限的 token。

## 本地构建

在 Windows x64、Python 3.12 和 Inno Setup 6 环境中运行：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.\packaging\build.ps1 -Python .\.venv-build\Scripts\python.exe
```

Inno Setup 不在标准位置时传 `-Iscc '完整路径\ISCC.exe'`；只打包可执行目录可加 `-BundleOnly`。产物在 `dist/installer/`。构建使用明确收集的源码、图标和依赖，不递归打包仓库或私人资料。

CI 在发布前运行冻结程序的离线冒烟：`DesktopGallery.exe --smoke` 初始化隔离数据的 Tk 界面；`DesktopGalleryLogin.exe --smoke-package` 用隐藏本地 HTML 验证 WebView2 JavaScript 执行，15 秒超时失败。两个进程均需正常退出，CI 另设 60 秒上限。登录脚本原有 `--smoke` 仍用于联网二维码页检查，不用于发布流水线。

发布工作流需要仓库启用 Actions，且策略允许 `contents: write`。Windows runner 安装 Inno Setup 6.7.3。安装包目前未代码签名；SHA-256 用于检查传输一致性，不是发布者数字签名。

## 参考

- [PyInstaller spec 文件与多程序共用依赖](https://pyinstaller.org/en/stable/spec-files.html)
- [Inno Setup AppMutex](https://jrsoftware.org/ishelp/topic_setup_appmutex.htm)
- [Inno Setup CloseApplications](https://jrsoftware.org/ishelp/topic_setup_closeapplications.htm)
- [GitHub Release 上传](https://cli.github.com/manual/gh_release_upload)
- [GitHub Release API 与 make_latest](https://docs.github.com/en/rest/releases/releases)
- [Windows 2022 runner 软件清单](https://github.com/actions/runner-images/blob/main/images/windows/Windows2022-Readme.md)
