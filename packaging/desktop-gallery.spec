from pathlib import Path

root = Path(SPECPATH).parent
icon = str(root / 'app' / 'icon.ico')

# 转换组件随安装包分发，桌面画廊不再依赖用户机器上另行安装的 FFmpeg。
# 先运行 packaging/prepare_ffmpeg.ps1 暂存 FFmpeg（固定版本并校验 SHA-256）。
converter_dir = root / 'tools' / 'ffmpeg' / 'bin'
converter_binaries = []
for name in ('ffmpeg.exe', 'ffprobe.exe'):
    tool = converter_dir / name
    if not tool.is_file():
        raise SystemExit(
            f'缺少随包转换组件 {tool}；请先运行 packaging/prepare_ffmpeg.ps1 暂存 FFmpeg'
        )
    converter_binaries.append((str(tool), 'tools/ffmpeg/bin'))
converter_datas = [(icon, '.')]
converter_license = root / 'tools' / 'ffmpeg' / 'LICENSE.txt'
if converter_license.is_file():
    converter_datas.append((str(converter_license), 'tools/ffmpeg'))

common = dict(pathex=[str(root / 'app')], binaries=converter_binaries,
              datas=converter_datas, hookspath=[], runtime_hooks=[],
              excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6'], noarchive=False)
main = Analysis([str(root / 'app' / 'native_wallpaper.py')],
                hiddenimports=['pystray._win32'], **common)
login = Analysis([str(root / 'app' / 'wechat_login_window.py')],
                 hiddenimports=['webview.platforms.winforms'], **common)
main_exe = EXE(PYZ(main.pure), main.scripts, [], exclude_binaries=True,
               name='DesktopGallery', console=False, icon=icon)
login_exe = EXE(PYZ(login.pure), login.scripts, [], exclude_binaries=True,
                name='DesktopGalleryLogin', console=True, icon=icon)
COLLECT(main_exe, login_exe, main.binaries, main.datas, login.binaries,
        login.datas, name='DesktopGallery')
