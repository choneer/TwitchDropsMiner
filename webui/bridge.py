"""Presentation port implementing the callbacks used by the upstream mining core.

No widgets, display server, VNC, or synthetic mining progress. Models retain their
original claim/channel logic; these adapters only observe and present state.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace


def drop_view(drop):
    return {
        'id': drop.id, 'name': drop.name, 'rewards': drop.rewards_text(),
        'minutes': drop.current_minutes, 'confirmed_minutes': drop.real_current_minutes,
        'required_minutes': drop.required_minutes, 'progress': drop.progress,
        'claimed': drop.is_claimed, 'claimable': drop.can_claim,
        'benefits': [{'name': b.name, 'image': str(b.image_url)} for b in drop.benefits],
    }


def channel_view(channel):
    return {'id': str(channel.id), 'name': channel.name, 'url': str(channel.url),
            'online': channel.online, 'viewers': channel.viewers,
            'game': channel.game.name if channel.game else None,
            'drops_enabled': channel.drops_enabled, 'priority': channel.acl_based}


class Status:
    def __init__(self):
        self.text = '等待启动'

    def update(self, text):
        self.text = str(text)

    def clear(self):
        self.text = ''


class Login:
    def __init__(self, bridge):
        self.bridge = bridge
        self.status = '未登录'
        self.user_id = None
        self.device = None

    async def ask_enter_code(self, page_url, user_code, *, expires_at=None):
        # Do not open a browser on the server or put activation codes in logs.
        self.device = {'code': user_code, 'url': 'https://www.twitch.tv/activate',
                       'issued_at': time.time(),
                       'expires_at': expires_at.isoformat() if expires_at else None}
        self.status = '等待设备授权'

    async def ask_login(self):
        raise RuntimeError('Web service only supports Twitch device authorization')

    def update(self, status, user_id):
        self.status, self.user_id = str(status), user_id
        if user_id is not None:
            self.device = None

    def clear(self, **kwargs):
        self.device = None


class InvalidateControl:
    def __init__(self):
        self.enabled = False

    def config(self, *, state):
        self.enabled = state == 'normal'


class Inventory:
    def __init__(self):
        self.campaigns = {}

    async def add_campaign(self, campaign):
        self.campaigns[campaign.id] = campaign

    def clear(self):
        self.campaigns.clear()

    def update_drop(self, drop):
        self.campaigns[drop.campaign.id] = drop.campaign


class Channels:
    def __init__(self):
        self.entries = {}
        self.watching = None
        self.selected = None

    def display(self, channel, *, add=False):
        self.entries[str(channel.id)] = channel

    def remove(self, channel):
        self.entries.pop(str(channel.id), None)
        if self.selected is channel:
            self.selected = None

    def clear(self):
        self.entries.clear()
        self.watching = self.selected = None

    def get_selection(self):
        return self.selected

    def clear_selection(self):
        self.selected = None

    def set_watching(self, channel):
        self.watching = channel

    def clear_watching(self):
        self.watching = None


class Progress:
    def __init__(self):
        self.drop = None
        self.deadline = None

    def stop_timer(self):
        self.deadline = None

    def minute_almost_done(self):
        return self.deadline is None or time.monotonic() >= self.deadline - 10

    def display(self, drop, *, countdown=True, subone=False):
        self.drop = drop
        self.deadline = time.monotonic() + 60 if countdown and drop and drop.remaining_minutes > 0 else None


class Websockets:
    def __init__(self):
        self.entries = {}

    def update(self, idx, status=None, topics=None):
        entry = self.entries.setdefault(idx, {})
        if status is not None:
            entry['status'] = str(status)
        if topics is not None:
            entry['topics'] = topics

    def remove(self, idx):
        self.entries.pop(idx, None)


class Tray:
    def __init__(self, service):
        self.service = service
        self.icon = 'idle'

    def change_icon(self, state):
        self.icon = state

    def notify(self, message, title=None, **kwargs):
        self.service.log('INFO', str(message))

    def update_title(self, drop):
        pass  # The web page derives its title/progress from the same model.


class WebBridge:
    def __init__(self, twitch, service):
        self._twitch, self.service = twitch, service
        self._closed = asyncio.Event()
        self.status, self.login = Status(), Login(self)
        self.inv, self.channels = Inventory(), Channels()
        self.progress, self.websockets = Progress(), Websockets()
        self.tray = Tray(service)
        self.help = SimpleNamespace(_invalidate_button=InvalidateControl())
        self.games = []

    @property
    def close_requested(self):
        return self._closed.is_set()

    @property
    def running(self):
        return not self.close_requested

    def start(self):
        pass  # aiohttp and the miner share one asyncio loop; no GUI loop needed.

    def stop(self):
        self.close()

    def close(self, *args):
        self._closed.set()
        self._twitch.close()

    def prevent_close(self):
        # Never discard a server shutdown request to keep an error window open.
        pass

    async def wait_until_closed(self):
        await self._closed.wait()

    async def coro_unless_closed(self, coro):
        task = asyncio.ensure_future(coro)
        closing = asyncio.create_task(self._closed.wait())
        try:
            await asyncio.wait([task, closing], return_when=asyncio.FIRST_COMPLETED)
            if self.close_requested:
                from exceptions import ExitRequest
                raise ExitRequest()
            return await task
        finally:
            for pending in (task, closing):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, closing, return_exceptions=True)

    def set_games(self, games):
        self.games = sorted(game.name for game in games)

    def display_drop(self, drop, *, countdown=True, subone=False):
        self.progress.display(drop, countdown=countdown, subone=subone)

    def clear_drop(self):
        self.progress.display(None)

    def print(self, message):
        self.service.log('INFO', str(message))

    def save(self, *, force=False):
        pass  # No desktop/image cache is created by this presentation.

    def grab_attention(self, **kwargs):
        pass  # Web UI shows the current error/status without OS notifications.

    def close_window(self):
        self.close()

    def snapshot(self):
        campaigns = []
        for c in self.inv.campaigns.values():
            campaigns.append({'id': c.id, 'name': c.name, 'game': c.game.name,
                'image': str(c.image_url), 'linked': c.linked, 'eligible': c.eligible,
                'starts_at': c.starts_at.isoformat(), 'ends_at': c.ends_at.isoformat(),
                'active': c.active, 'upcoming': c.upcoming, 'expired': c.expired,
                'link_url': c.link_url, 'drops': [drop_view(d) for d in c.drops]})
        logged_in = getattr(self._twitch._auth_state, '_logged_in', None)
        return {'status': self.status.text, 'account': {'status': self.login.status,
                'authenticated': bool(logged_in and logged_in.is_set()),
                'user_id': self.login.user_id, 'device': self.login.device},
                'games': self.games, 'campaigns': campaigns,
                'channels': [channel_view(c) for c in self.channels.entries.values()],
                'watching': channel_view(self.channels.watching) if self.channels.watching else None,
                'drop': drop_view(self.progress.drop) if self.progress.drop else None,
                'websockets': list(self.websockets.entries.values())}
