"""Offline tests: only temporary files, fake workers and loopback HTTP."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

# Set paths before core imports. Never touch a checkout's or NAS's real data.
_bootstrap = tempfile.TemporaryDirectory()
os.environ['TDM_DATA_DIR'] = _bootstrap.name
sys.argv[0] = str(Path.cwd() / 'main.py')

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from webui.server import create_app
from webui.service import MinerService
from webui.bridge import WebBridge
from constants import PriorityMode

PASSWORD = 'offline-password-for-unit-tests'


class FakeWorker:
    def __init__(self, settings, *, gui_factory):
        self.settings = settings
        self._auth_state = SimpleNamespace()
        self.gui = gui_factory(self)
        self.shutdown_called = False

    async def run(self):
        self.gui.status.update('Fake worker waiting (offline)')
        await self.gui.wait_until_closed()

    def close(self):
        pass

    async def shutdown(self):
        self.shutdown_called = True

    def save(self, *, force=False):
        self.settings.save(force=force)


class FailingWorker(FakeWorker):
    async def run(self):
        raise RuntimeError('test failure password=' + PASSWORD + ' OAuth sensitive-token')


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.settings_patch = patch('settings.SETTINGS_PATH', self.directory / 'settings.json')
        self.settings_patch.start()
        self.service = MinerService(self.directory, FakeWorker, PASSWORD)

    async def asyncTearDown(self):
        await self.service.close()
        self.settings_patch.stop()
        self.tmp.cleanup()

    async def test_start_stop_single_worker_and_save(self):
        self.assertEqual(self.service.phase, 'stopped')
        await self.service.start()
        original = self.service.task
        await self.service.start()
        self.assertIs(self.service.task, original)
        await asyncio.sleep(0)
        self.assertEqual(self.service.phase, 'running')
        await self.service.stop()
        self.assertTrue(self.service.client.shutdown_called)
        self.assertFalse(self.service.autostart)
        self.assertTrue((self.directory / 'settings.json').exists())

    async def test_autostart_intent_survives_server_shutdown(self):
        await self.service.start()
        await self.service.stop(preserve_intent=True)
        self.assertTrue(json.loads(self.service.intent_path.read_text())['autostart'])

    async def test_reload_replaces_worker(self):
        await self.service.start()
        old = self.service.client
        await self.service.reload()
        self.assertIsNot(old, self.service.client)
        self.assertTrue(old.shutdown_called)

    async def test_web_default_explicitly_injects_smartbox(self):
        from constants import ClientType
        worker = FakeWorker(self.service.settings, gui_factory=lambda c: WebBridge(c, self.service))
        self.service.worker_factory = None
        with patch('twitch.Twitch', return_value=worker) as factory:
            await self.service.start()
            self.assertIs(factory.call_args.kwargs['client_type'], ClientType.SMARTBOX)
        await self.service.stop()

    async def test_error_observable_and_redacted_without_restart_loop(self):
        self.service.worker_factory = FailingWorker
        await self.service.start()
        await self.service.task
        self.assertEqual(self.service.phase, 'error')
        self.assertNotIn(PASSWORD, self.service.error)
        self.assertNotIn('sensitive-token', self.service.error)
        self.assertIn('RuntimeError', self.service.error)
        self.assertNotIn(PASSWORD, (self.directory / 'web-logs/application.log').read_text(encoding='utf-8'))
        self.assertTrue(self.service.task.done())

    async def test_logs_bounded(self):
        with patch('builtins.print'):
            for number in range(550):
                self.service.log('INFO', str(number))
        self.assertEqual(len(self.service.logs), 500)
        self.assertEqual(self.service.logs[0]['message'], '50')

    async def test_settings_validated_before_mutating(self):
        before = self.service.settings.connection_quality
        with self.assertRaises(ValueError):
            self.service.update_settings({'priority': ['A'], 'connection_quality': 999})
        self.assertEqual(self.service.settings.priority, [])
        self.assertEqual(before, self.service.settings.connection_quality)
        for invalid in ({'priority': 'bad'}, {'connection_quality': True}, {'proxy': 'http://bad'},
                        {'priority_mode': 'not-a-mode'}, {'language': 'missing'}, {'enable_badges_emotes': 1}):
            with self.assertRaises(ValueError):
                self.service.update_settings(invalid)

    async def test_settings_roundtrip_original_format(self):
        self.service.update_settings({'priority': ['A', 'B', 'A'], 'exclude': ['C'],
                                     'priority_mode': 'ENDING_SOONEST'})
        from settings import Settings
        restored = Settings(SimpleNamespace())
        self.assertEqual(restored.priority, ['A', 'B'])
        self.assertEqual(restored.exclude, {'C'})
        self.assertEqual(restored.priority_mode, PriorityMode.ENDING_SOONEST)

    async def test_bridge_cancels_network_wait_on_stop(self):
        await self.service.start()
        bridge = self.service.client.gui
        task = asyncio.create_task(bridge.coro_unless_closed(asyncio.sleep(100)))
        await asyncio.sleep(0)
        bridge.close()
        from exceptions import ExitRequest
        with self.assertRaises(ExitRequest):
            await task

    async def test_device_code_not_in_logs_but_available_to_authenticated_ui(self):
        await self.service.start()
        bridge = self.service.client.gui
        await bridge.login.ask_enter_code('https://www.twitch.tv/activate?device-code=SECRET', 'SECRETCODE')
        self.service.log('INFO', 'SECRETCODE')
        self.assertEqual(self.service.logs[-1]['message'], '<redacted>')
        self.assertEqual(self.service.snapshot()['account']['device']['code'], 'SECRETCODE')
        await self.service.stop()
        self.assertIsNone(self.service.snapshot()['account']['device'])

    async def test_real_core_imports_without_desktop_dependencies_and_cookie_migration(self):
        from twitch import Twitch
        self.assertNotIn('tkinter', sys.modules)
        self.assertNotIn('pystray', sys.modules)
        self.assertNotIn('gui', sys.modules)
        path = self.directory / 'cookies.jar'
        from yarl import URL
        jar = aiohttp.CookieJar()
        jar.update_cookies({'auth-token': 'fake-cookie', 'unique_id': 'fake-device'}, URL('https://www.twitch.tv'))
        jar.save(path)
        client = Twitch(self.service.settings, gui_factory=lambda c: WebBridge(c, self.service))
        with patch('twitch.COOKIES_PATH', path):
            session = await client.get_session()
            try:
                cookies = session.cookie_jar.filter_cookies(URL('https://www.twitch.tv'))
                self.assertEqual(cookies['auth-token'].value, 'fake-cookie')
            finally:
                await session.close()


class ApiTests(ServiceTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = TestClient(TestServer(create_app(self.service, PASSWORD)),
                                 cookie_jar=aiohttp.CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await super().asyncTearDown()

    async def login(self):
        response = await self.client.post('/api/login', json={'password': PASSWORD})
        self.assertEqual(response.status, 200)
        body = await response.json()
        return {'X-CSRF-Token': body['csrf']}

    async def test_auth_and_no_redirect_to_internal_port(self):
        response = await self.client.get('/')
        self.assertEqual(response.status, 200)
        self.assertNotIn('vnc', (await response.text()).lower())
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual((await self.client.get('/api/state')).status, 401)
        self.assertEqual((await self.client.get('/healthz')).status, 200)
        headers = await self.login()
        self.assertEqual((await self.client.get('/api/state')).status, 200)
        self.assertEqual((await self.client.post('/api/actions/start')).status, 403)
        self.assertEqual((await self.client.post('/api/actions/start', headers=headers)).status, 200)
        self.assertEqual((await self.client.post('/api/logout', headers=headers)).status, 200)
        self.assertEqual((await self.client.get('/api/state')).status, 401)

    async def test_cross_origin_login_and_mutations_rejected(self):
        response = await self.client.post('/api/login', json={'password': PASSWORD}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)
        headers = await self.login()
        response = await self.client.put('/api/settings', json={'priority': ['A']}, headers={**headers, 'Origin': 'https://evil.example'})
        self.assertEqual(response.status, 403)

    async def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual((await self.client.post('/api/login', json={'password': 'bad'})).status, 401)
        self.assertEqual((await self.client.post('/api/login', json={'password': PASSWORD})).status, 429)

    async def test_json_only_and_validation(self):
        self.assertEqual((await self.client.post('/api/login', data={'password': PASSWORD})).status, 415)
        self.assertEqual((await self.client.post('/api/login', json=[])).status, 400)
        headers = await self.login()
        self.assertEqual((await self.client.put('/api/settings', json={'priority': ['A']}, headers=headers)).status, 200)
        self.assertEqual((await self.client.put('/api/settings', json={'connection_quality': 0}, headers=headers)).status, 400)
        self.assertEqual((await self.client.get('/assets/unknown.txt')).status, 404)
        self.assertEqual((await self.client.post('/api/actions/unknown', headers=headers)).status, 404)

    async def test_cookie_flags_and_session_bootstrap(self):
        response = await self.client.post('/api/login', json={'password': PASSWORD})
        cookie = response.cookies['tdm_session']
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Strict')
        self.assertEqual((await self.client.get('/api/session')).status, 200)


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    def make_auth(self, replies):
        from twitch import _AuthState
        from constants import ClientType
        responses = iter(replies)
        @asynccontextmanager
        async def request(method, url, **kwargs):
            status, payload = next(responses)
            async def as_json():
                if isinstance(payload, Exception):
                    raise payload
                return payload
            yield SimpleNamespace(status=status, json=as_json)
        async def passthrough(coro):
            return await coro
        client = SimpleNamespace(gui=SimpleNamespace(login=SimpleNamespace(ask_enter_code=AsyncMock()),
            coro_unless_closed=passthrough), _client_type=ClientType.SMARTBOX, request=request)
        auth = _AuthState(client)
        auth.device_id = 'fixture-device'
        return auth

    def valid_device(self):
        return {'device_code': 'fixture-device-code', 'user_code': 'FIXTURE', 'interval': 1,
                'expires_in': 1800, 'verification_uri': 'https://www.twitch.tv/activate'}

    async def test_invalid_client_reports_status_without_raw_body(self):
        from exceptions import LoginException
        auth = self.make_auth([(400, {'message': 'invalid client', 'device_code': 'never-log-this'})])
        with self.assertRaises(LoginException) as caught:
            await auth._oauth_login()
        self.assertIn('HTTP 400, invalid client', str(caught.exception))
        self.assertNotIn('never-log-this', str(caught.exception))

    async def test_missing_or_invalid_device_fields_are_not_keyerrors(self):
        from exceptions import LoginException
        for payload in ({}, [], {'device_code': None},
                        {**self.valid_device(), 'interval': True},
                        {**self.valid_device(), 'expires_in': -1},
                        {**self.valid_device(), 'verification_uri': 'https://evil.example/activate'}):
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(LoginException):
                    await self.make_auth([(200, payload)])._oauth_login()

    async def test_non_json_response_is_sanitized(self):
        from exceptions import LoginException
        auth = self.make_auth([(400, ValueError('secret body should not be logged'))])
        with self.assertRaises(LoginException) as caught:
            await auth._oauth_login()
        self.assertIn('response is not JSON', str(caught.exception))
        self.assertNotIn('secret body', str(caught.exception))

    async def test_unknown_remote_error_not_echoed(self):
        from exceptions import LoginException
        auth = self.make_auth([(403, {'message': 'secret remote value'})])
        with self.assertRaises(LoginException) as caught:
            await auth._oauth_login()
        self.assertIn('unrecognized OAuth error', str(caught.exception))
        self.assertNotIn('secret remote value', str(caught.exception))

    async def test_missing_token_is_explicit_error(self):
        from exceptions import LoginException
        auth = self.make_auth([(200, self.valid_device()), (200, {})])
        with patch('twitch.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(LoginException) as caught:
                await auth._oauth_login()
        self.assertIn('missing or invalid access_token', str(caught.exception))

    async def test_pending_and_slow_down_then_success(self):
        auth = self.make_auth([(200, self.valid_device()),
            (400, {'message': 'authorization_pending'}), (400, {'error': 'slow_down'}),
            (200, {'access_token': 'fixture-success-token'})])
        with patch('twitch.asyncio.sleep', new=AsyncMock()) as sleeper:
            self.assertEqual(await auth._oauth_login(), 'fixture-success-token')
        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [1, 1, 6])

    async def test_expired_code_restarts_with_correct_device_payload(self):
        from twitch import _AuthState
        from constants import ClientType
        requests = []
        responses = iter([
            (200, {'device_code': 'device1', 'user_code': 'USER1', 'interval': 1,
                   'expires_in': 1800, 'verification_uri': 'https://www.twitch.tv/activate'}),
            (400, {'error': 'expired_token'}),
            (200, {'device_code': 'device2', 'user_code': 'USER2', 'interval': 1,
                   'expires_in': 1800, 'verification_uri': 'https://www.twitch.tv/activate'}),
            (200, {'access_token': 'fixture-auth-token'}),
        ])
        @asynccontextmanager
        async def request(method, url, **kwargs):
            requests.append((url, dict(kwargs['data'])))
            status, payload = next(responses)
            async def as_json():
                return payload
            yield SimpleNamespace(status=status, json=as_json)
        async def passthrough(coro):
            return await coro
        gui = SimpleNamespace(login=SimpleNamespace(ask_enter_code=AsyncMock()),
                              coro_unless_closed=passthrough)
        client = SimpleNamespace(gui=gui, _client_type=ClientType.ANDROID_APP, request=request)
        auth = _AuthState(client)
        auth.device_id = 'fixture-device'
        with patch('twitch.asyncio.sleep', new=AsyncMock()):
            self.assertEqual(await auth._oauth_login(), 'fixture-auth-token')
        self.assertEqual(requests[0][1], requests[2][1])
        self.assertNotIn('device_code', requests[2][1])
        self.assertEqual(requests[3][1]['device_code'], 'device2')


if __name__ == '__main__':
    unittest.main()
