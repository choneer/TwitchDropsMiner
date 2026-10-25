"""Authenticated native WebUI. One account, one worker, no desktop transport."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from collections import OrderedDict

from aiohttp import web

STATIC = Path(__file__).parent / 'static'
COOKIE = 'tdm_session'
SESSION_TTL = 12 * 60 * 60
SESSION_KEY = web.RequestKey('tdm.session', dict) if hasattr(web, 'RequestKey') else 'tdm.session'


def create_app(service, password, *, secure_cookies=False, public_origin=None):
    if not 16 <= len(password.encode()) <= 72:
        raise ValueError('Web password must contain 16–72 bytes')
    sessions, failures = OrderedDict(), OrderedDict()

    def check_origin(request):
        origin = request.headers.get('Origin')
        expected = public_origin or f'{request.scheme}://{request.host}'
        if origin and origin != expected:
            raise web.HTTPForbidden(text='Origin mismatch')

    @web.middleware
    async def security(request, handler):
        if request.path.startswith('/api/') and request.path != '/api/login':
            session = sessions.get(request.cookies.get(COOKIE, ''))
            if session is None or session['expires'] <= time.monotonic():
                raise web.HTTPUnauthorized(text='请先登录')
            request[SESSION_KEY] = session
            if request.method not in ('GET', 'HEAD'):
                check_origin(request)
                if not hmac.compare_digest(request.headers.get('X-CSRF-Token', '').encode(), session['csrf'].encode()):
                    raise web.HTTPForbidden(text='CSRF token required')
        response = await handler(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https://static-cdn.jtvnw.net https://images.igdb.com; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    app = web.Application(middlewares=[security], client_max_size=64*1024)

    async def json_body(request):
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType(text='JSON required')
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text='Invalid JSON') from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='JSON object required')
        return data

    async def login(request):
        check_origin(request)
        data = await json_body(request)
        peer, now = request.remote or 'unknown', time.monotonic()
        attempts, since = failures.get(peer, (0, now))
        if now - since >= 60:
            attempts, since = 0, now
        if attempts >= 5:
            raise web.HTTPTooManyRequests(text='尝试过于频繁，请一分钟后重试')
        supplied = data.get('password', '')
        if not isinstance(supplied, str) or data.get('username', 'admin') != 'admin' or not hmac.compare_digest(supplied.encode(), password.encode()):
            failures[peer] = (attempts + 1, since)
            if len(failures) > 4096:
                failures.popitem(last=False)
            raise web.HTTPUnauthorized(text='用户名或密码错误')
        failures.pop(peer, None)
        for key in list(sessions):
            if sessions[key]['expires'] <= now:
                del sessions[key]
        if len(sessions) >= 128:
            sessions.popitem(last=False)
        identifier = secrets.token_urlsafe(32)
        session = {'csrf': secrets.token_urlsafe(32), 'expires': now + SESSION_TTL}
        sessions[identifier] = session
        response = web.json_response({'csrf': session['csrf']})
        response.set_cookie(COOKIE, identifier, max_age=SESSION_TTL, httponly=True,
                            secure=secure_cookies, samesite='Strict', path='/')
        return response

    async def session_view(request):
        return web.json_response({'csrf': request[SESSION_KEY]['csrf'], 'username': 'admin'})

    async def logout(request):
        sessions.pop(request.cookies.get(COOKIE, ''), None)
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path='/')
        return response

    async def state(request):
        return web.json_response(service.snapshot())

    async def settings(request):
        try:
            service.update_settings(await json_body(request))
        except (TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return web.json_response({'ok': True})

    action_lock = asyncio.Lock()

    async def action(request):
        name = request.match_info['name']
        if name not in ('start', 'stop', 'reload', 'select-channel'):
            raise web.HTTPNotFound()
        if action_lock.locked():
            raise web.HTTPConflict(text='另一项操作正在进行，请稍后')
        async with action_lock:
            if name == 'select-channel':
                data = await json_body(request)
                if not service.client or service.phase != 'running':
                    raise web.HTTPConflict(text='后台未运行')
                channel = service.client.gui.channels.entries.get(str(data.get('id')))
                if channel is None:
                    raise web.HTTPBadRequest(text='频道不可用')
                from constants import State
                service.client.gui.channels.selected = channel
                service.client.change_state(State.CHANNEL_SWITCH)
            else:
                await getattr(service, name)()
        return web.json_response({'ok': True})

    async def health(request):
        # Deliberately does not claim Twitch success or disclose account details.
        return web.json_response({'service': 'ok', 'interface': 'native-web'})

    async def index(request):
        return web.FileResponse(STATIC / 'index.html')

    async def asset(request):
        name = request.match_info['name']
        if name not in ('app.js', 'app.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def lifecycle(app):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        def report_background_error(loop, context):
            import traceback
            exc = context.get('exception')
            text = ''.join(traceback.format_exception(exc)) if exc else str(context.get('message', 'Background error'))
            service.log('ERROR', text)
        loop.set_exception_handler(report_background_error)
        try:
            if service.autostart:
                await service.start()
            yield
        finally:
            await service.close()
            loop.set_exception_handler(previous)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get('/', index)
    app.router.add_get('/assets/{name}', asset)
    app.router.add_get('/healthz', health)
    app.router.add_post('/api/login', login)
    app.router.add_post('/api/logout', logout)
    app.router.add_get('/api/session', session_view)
    app.router.add_get('/api/state', state)
    app.router.add_put('/api/settings', settings)
    app.router.add_post('/api/actions/{name}', action)
    return app


def main():
    import truststore
    truststore.inject_into_ssl()
    from constants import DATA_DIR, LOCK_PATH
    from utils import lock_file
    from webui.service import MinerService
    os.umask(0o077)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    password = Path(os.environ.get('WEB_PASSWORD_FILE', '/run/secrets/web_password')).read_text().rstrip('\n')
    if not 16 <= len(password.encode()) <= 72 or not password.isascii() or any(ord(c) < 32 for c in password):
        raise ValueError('Web password must contain 16–72 printable ASCII bytes')
    success, lock = lock_file(LOCK_PATH)
    if not success:
        lock.close()
        raise RuntimeError('Another miner is using this data directory; refusing concurrent access')
    service = MinerService(password=password)
    try:
        web.run_app(create_app(service, password,
            secure_cookies=os.environ.get('TDM_SECURE_COOKIES') == '1',
            public_origin=os.environ.get('TDM_PUBLIC_ORIGIN')), host='0.0.0.0', port=8080,
            access_log=None, shutdown_timeout=120)
    finally:
        lock.close()


if __name__ == '__main__':
    main()
