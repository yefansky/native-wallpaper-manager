import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'app'))
import updater
import product
import native_wallpaper as app


def response(data=b'', status=200, location=None):
    result = Mock(status_code=status, headers={'Location': location} if location else {})
    result.iter_content.return_value = [data]
    return result


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.client = updater.GitHubUpdater('test_secret')
        self.client.session = Mock()
        self.name = 'DesktopGallery-0.1.12-Setup.exe'
        self.release = updater.Release('0.1.12', self.name,
                                       self.client.api + '/assets/1', self.client.api + '/assets/2', 7)

    def payload(self, version='0.1.12'):
        return {'tag_name': 'v' + version, 'assets': [
            {'name': self.name, 'url': self.release.installer_url, 'size': 7, 'state': 'uploaded'},
            {'name': self.name + '.sha256', 'url': self.release.checksum_url, 'state': 'uploaded'}]}

    def test_numeric_version_and_downgrade(self):
        self.assertGreater(updater.version_tuple('0.1.12'), updater.version_tuple('0.1.9'))
        self.client.session.get.return_value = response(json.dumps(self.payload('0.1.8')).encode())
        self.assertIsNone(self.client.check('0.1.9'))
        for invalid in ['1.2', '1.2.3-beta', '../1.2.3']:
            with self.assertRaises(updater.UpdateError):
                updater.version_tuple(invalid)

    def test_release_missing_assets_and_foreign_repository(self):
        payload = self.payload()
        payload['assets'][0]['url'] = 'https://api.github.com/repos/attacker/repo/releases/assets/1'
        self.client.session.get.return_value = response(json.dumps(payload).encode())
        with self.assertRaises(updater.UpdateError):
            self.client.check()

    def test_anonymous_check_succeeds_without_authorization(self):
        # 公开仓库匿名读取 Release：空 token 也能检查，且请求头不带 Authorization。
        client = updater.GitHubUpdater('')
        client.session = Mock()
        name = 'DesktopGallery-0.1.12-Setup.exe'
        release = updater.Release('0.1.12', name, client.api + '/assets/1', client.api + '/assets/2', 7)
        payload = {'tag_name': 'v0.1.12', 'assets': [
            {'name': name, 'url': release.installer_url, 'size': 7, 'state': 'uploaded'},
            {'name': name + '.sha256', 'url': release.checksum_url, 'state': 'uploaded'}]}
        client.session.get.return_value = response(json.dumps(payload).encode())

        found = client.check('0.1.11')

        self.assertIsNotNone(found)
        self.assertEqual(found.version, '0.1.12')
        self.assertEqual(client.session.get.call_args.args[0], client.api + '/latest')
        self.assertNotIn('Authorization', client.session.get.call_args.kwargs['headers'])

    def test_credentials_stripped_on_cdn_redirect(self):
        self.client.session.get.side_effect = [
            response(status=302, location='https://release-assets.githubusercontent.com/file?signature=value'), response(b'body')]
        self.client._get(self.release.installer_url, True)
        calls = self.client.session.get.call_args_list
        self.assertEqual(calls[0].kwargs['headers']['Authorization'], 'Bearer test_secret')
        self.assertNotIn('Authorization', calls[1].kwargs['headers'])

    def test_proxy_environment_preserved_without_netrc(self):
        client = updater.GitHubUpdater()
        self.assertTrue(client.session.trust_env)
        with patch('requests.sessions.get_netrc_auth', return_value=('user', 'secret')) as netrc:
            request = client.session.prepare_request(updater.requests.Request('GET', 'https://release-assets.githubusercontent.com/file'))
        netrc.assert_not_called()
        self.assertNotIn('Authorization', request.headers)

    def test_rejects_untrusted_redirect_before_request(self):
        for url in ['https://evil.example/file', 'http://release-assets.githubusercontent.com/file',
                    'https://api.github.com/repos/other/repo/releases/assets/1']:
            self.client.session.get.reset_mock()
            self.client.session.get.side_effect = [response(status=302, location=url)]
            with self.assertRaises(updater.UpdateError):
                self.client._get(self.release.installer_url, True)
            self.assertEqual(self.client.session.get.call_count, 1)

    def test_download_verified_and_partial_removed_on_failure(self):
        for content, good in [(b'payload', True), (b'corrupt', False), (b'longer-than-size', False)]:
            checksum = (hashlib.sha256(b'payload').hexdigest() + '  ' + self.name).encode()
            self.client.session.get.side_effect = [response(checksum), response(content)]
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory) / 'update'
                folder.mkdir()
                with patch.object(updater.tempfile, 'mkdtemp', return_value=str(folder)):
                    if good:
                        target = self.client.download(self.release)
                        self.assertEqual(target.read_bytes(), content)
                        self.assertFalse(list(folder.glob('*.partial')))
                    else:
                        with self.assertRaises(updater.UpdateError):
                            self.client.download(self.release)
                        self.assertFalse(folder.exists())

    def test_helper_waits_before_launch_and_quotes_paths(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(updater.subprocess, 'Popen') as popen:
            updater.launch_installer(Path("C:/Users/O'Brien/update package.exe"))
        command = popen.call_args.args[0]
        script = base64.b64decode(command[-1]).decode('utf-16le')
        self.assertLess(script.index('Wait-Process'), script.index('Start-Process'))
        self.assertIn("O''Brien", script)
        self.assertIn('-Timeout 120', script)
        self.assertIn('Hidden', command)

    def test_frozen_autostart_uses_exe_only(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', 'C:/Program Files/Gallery/DesktopGallery.exe'):
            self.assertEqual(product.autostart_command(), '"C:/Program Files/Gallery/DesktopGallery.exe"')

    def test_private_token_is_encrypted_and_clearable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(updater, 'TOKEN_PATH', Path(directory) / 'token.enc'):
            updater.save_update_token('github_pat_example123')
            self.assertNotIn(b'github_pat_example123', updater.TOKEN_PATH.read_bytes())
            self.assertEqual(updater.load_update_token(), 'github_pat_example123')
            updater.save_update_token('')
            self.assertEqual(updater.load_update_token(), '')

    def test_installer_launch_failure_keeps_app_open(self):
        instance = Mock(_apply_operation_in_progress=False, _loading_online=False)
        instance._ui_queue.empty.return_value = True
        with patch.object(sys, 'frozen', True, create=True), patch.object(app.messagebox, 'askyesno', return_value=True), patch.object(app, 'launch_installer', side_effect=OSError('failed')):
            app.App._update_downloaded(instance, Path('setup.exe'))
        instance._quit_app.assert_not_called()
        instance._update_failed.assert_called_once()

    def test_installer_handoff_persists_before_exit(self):
        instance = Mock(_apply_operation_in_progress=False, _loading_online=False)
        instance._ui_queue.empty.return_value = True
        order = []
        instance.persist.side_effect = lambda: order.append('save')
        instance._quit_app.side_effect = lambda: order.append('exit')
        with patch.object(sys, 'frozen', True, create=True), patch.object(app.messagebox, 'askyesno', return_value=True), patch.object(app, 'launch_installer', side_effect=lambda p: order.append('launch')):
            app.App._update_downloaded(instance, Path('setup.exe'))
        self.assertEqual(order, ['save', 'launch', 'exit'])


if __name__ == '__main__':
    unittest.main()
