from pathlib import Path

root = Path(SPECPATH).parent
icon = str(root / 'app' / 'icon.ico')
common = dict(pathex=[str(root / 'app')], binaries=[],
              datas=[(icon, '.')], hookspath=[], runtime_hooks=[],
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
