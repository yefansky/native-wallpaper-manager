"""GitHub Releases updates. Credentials never follow redirects or enter logs."""
from __future__ import annotations

import hashlib
import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from app_version import VERSION, RELEASE_REPOSITORY

MAX_INSTALLER = 1024 * 1024 * 1024
TOKEN_PATH = Path.home() / 'AppData' / 'Local' / 'NativeWallpaperManager' / 'github-update.enc'


class UpdateError(Exception):
    pass


class _NoNetrcAuth(requests.auth.AuthBase):
    def __call__(self, request):
        return request


def version_tuple(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise UpdateError('发布版本号格式无效。')
    return tuple(map(int, value.split('.')))


def save_update_token(token: str) -> None:
    from session_store import _encrypt
    token = token.strip()
    if not token:
        TOKEN_PATH.unlink(missing_ok=True)
        return
    if not re.fullmatch(r'[A-Za-z0-9_]+', token):
        raise UpdateError('GitHub 令牌格式无效。')
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_bytes(_encrypt(token.encode('utf-8')))


def load_update_token() -> str:
    from session_store import _decrypt
    try:
        return _decrypt(TOKEN_PATH.read_bytes()).decode('utf-8')
    except (OSError, UnicodeError):
        return ''


@dataclass(frozen=True)
class Release:
    version: str
    name: str
    installer_url: str
    checksum_url: str
    size: int


class GitHubUpdater:
    def __init__(self, token: str = '', repository: str = RELEASE_REPOSITORY):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise UpdateError('更新仓库配置无效。')
        self.repository = repository
        self.token = token
        self.api = f'https://api.github.com/repos/{repository}/releases'
        self.session = requests.Session()
        # Avoid implicit netrc credentials being forwarded to an asset CDN.
        self.session.auth = _NoNetrcAuth()

    def _get(self, url: str, binary: bool = False):
        for _ in range(6):
            parsed = urlsplit(url)
            host = parsed.hostname
            api = host == 'api.github.com'
            if (parsed.scheme != 'https' or parsed.username or parsed.password
                    or parsed.port not in (None, 443)
                    or host not in {'api.github.com', 'release-assets.githubusercontent.com',
                                    'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}
                    or (api and not parsed.path.startswith(f'/repos/{self.repository}/releases/'))):
                raise UpdateError('更新下载地址不属于指定 GitHub 仓库或可信资产服务。')
            headers = {'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
                       'User-Agent': 'DesktopGallery-Updater', 'X-GitHub-Api-Version': '2022-11-28'}
            if api and self.token:
                headers['Authorization'] = f'Bearer {self.token}'
            try:
                response = self.session.get(url, headers=headers, timeout=(10, 30),
                                            allow_redirects=False, stream=True)
            except requests.RequestException:
                raise UpdateError('无法连接 GitHub，请检查网络后重试。') from None
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location', '')
                response.close()
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                if status in (401, 403, 404):
                    raise UpdateError('尚无可用 Release，或更新源访问受限；请稍后重试。')
                raise UpdateError(f'GitHub 更新服务暂不可用（HTTP {status}）。')
            return response
        raise UpdateError('GitHub 下载重定向次数过多。')

    @staticmethod
    def _chunks(response, limit: int):
        total = 0
        deadline = time.monotonic() + 900
        try:
            for chunk in response.iter_content(128 * 1024):
                total += len(chunk)
                if total > limit or time.monotonic() > deadline:
                    raise UpdateError('更新文件过大或下载超时。')
                yield chunk
        except requests.RequestException:
            raise UpdateError('更新下载中断，请重试；当前版本未修改。') from None
        finally:
            response.close()

    def check(self, current: str = VERSION) -> Release | None:
        import json
        try:
            payload = json.loads(b''.join(self._chunks(self._get(self.api + '/latest'), 2 * 1024 * 1024)))
            if payload.get('draft') or payload.get('prerelease'):
                return None
            tag = payload['tag_name']
            version = tag.removeprefix('v')
            if version_tuple(version) <= version_tuple(current):
                return None
            name = f'DesktopGallery-{version}-Setup.exe'
            assets = {a['name']: a for a in payload['assets'] if a.get('state') == 'uploaded'}
            installer, checksum = assets[name], assets[name + '.sha256']
            size = installer['size']
            if not isinstance(size, int) or not 0 < size <= MAX_INSTALLER:
                raise UpdateError('安装包大小无效。')
            urls = [installer['url'], checksum['url']]
            if any(not re.fullmatch(re.escape(self.api) + r'/assets/\d+', u) for u in urls):
                raise UpdateError('Release 资产不属于指定仓库。')
            return Release(version, name, *urls, size)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UpdateError('GitHub Release 元数据不完整或无效。') from None

    def download(self, release: Release) -> Path:
        try:
            checksum = b''.join(self._chunks(self._get(release.checksum_url, True), 4096)).decode('ascii').strip()
        except UnicodeError:
            raise UpdateError('安装包校验文件无效。') from None
        match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?' + re.escape(release.name), checksum)
        if not match:
            raise UpdateError('安装包校验文件无效。')
        folder = Path(tempfile.mkdtemp(prefix='DesktopGallery-update-'))
        partial = folder / (release.name + '.partial')
        target = folder / release.name
        digest = hashlib.sha256()
        try:
            with partial.open('xb') as output:
                for chunk in self._chunks(self._get(release.installer_url, True), release.size):
                    output.write(chunk)
                    digest.update(chunk)
            if partial.stat().st_size != release.size or digest.hexdigest() != match[1].lower():
                raise UpdateError('安装包完整性校验失败，已拒绝安装；当前版本未修改。')
            partial.replace(target)
            return target
        except Exception:
            partial.unlink(missing_ok=True)
            folder.rmdir()
            raise


def launch_installer(path: Path) -> None:
    if not getattr(sys, 'frozen', False):
        raise UpdateError('源码运行模式请手动运行安装包；应用内更新用于已安装版本。')
    # A system helper owns the wait, so no packaged exe remains locked by it.
    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
    args = ['/SILENT', '/NORESTART', '/RESTARTAPP',
            '"/DIR=' + str(Path(sys.executable).parent) + '"']
    script = (
        f"$ErrorActionPreference='Stop'; $p=Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
        "try { if ($p) { $p | Wait-Process -Timeout 120 -ErrorAction Stop }; "
        f"$installer=Start-Process -Wait -PassThru -FilePath {ps_quote(str(path))} -ArgumentList "
        + ','.join(ps_quote(arg) for arg in args)
        + "; if ($installer.ExitCode -ne 0) { throw 'Installer failed' }"
        + " } catch { Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.MessageBox]::Show('Update did not complete. Please restart Desktop Gallery and retry. Your wallpaper library is preserved.', 'Desktop Gallery') | Out-Null; "
        + f"if (-not (Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue)) {{ Start-Process -FilePath {ps_quote(sys.executable)} }} }}"
    )
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                      '-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode('ascii')],
                     creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
