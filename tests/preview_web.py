"""Loopback-only visual QA with explicit offline fixtures. Never contacts Twitch."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
scratch = tempfile.TemporaryDirectory()
os.environ['TDM_DATA_DIR'] = scratch.name
sys.argv[0] = str(root / 'web_main.py')
from aiohttp import web
from yarl import URL
from inventory import DropsCampaign
from channel import Channel, Stream
from webui.service import MinerService
from webui.server import create_app


class PreviewWorker:
    def __init__(self, settings, *, gui_factory):
        self.settings = settings
        self._auth_state = SimpleNamespace(_logged_in=asyncio.Event())
        self._client_type = SimpleNamespace(CLIENT_URL=URL('https://www.twitch.tv'))
        self.gui = gui_factory(self)

    async def run(self):
        now = datetime.now(timezone.utc)
        def iso(value):
            return value.strftime('%Y-%m-%dT%H:%M:%SZ')
        self._auth_state._logged_in.set()
        self.gui.login.update('离线测试账号（不是 Twitch 实际账号）', 123456)
        self.gui.status.update('离线视觉测试 · 不连接 Twitch、不获取真实奖励')
        self.discovery = {'source': 'public_catalog', 'status': 'ok', 'count': 14,
                          'updated_at': now.isoformat(), 'message': '离线测试目录 · 不是真实 Twitch 活动'}
        names = ['离线演示 · 远行之境', '离线演示 · 星海航线'] + [f'离线演示 · 游戏 {i}' for i in range(12)]
        for index, game in enumerate(names):
            data = {'id': str(index), 'name': '探索者奖励季 · 离线测试数据',
                'game': {'id': str(9999+index), 'name': game, 'boxArtURL': ''},
                'self': {'isAccountConnected': True}, 'accountLinkURL': '',
                'startAt': iso(now-timedelta(days=1)), 'endAt': iso(now+timedelta(days=5)),
                'status': 'ACTIVE', 'allow': {'channels': [], 'isEnabled': False},
                'timeBasedDrops': []}
            for number in range(6 if index == 1 else 2):
                data['timeBasedDrops'].append({'id': f'{index}-{number}', 'name': '测试奖励',
                    'benefitEdges': [{'benefit': {'id': f'benefit-{index}-{number}',
                        'name': ['探索者补给箱', '星辉限定徽章'][number % 2],
                        'distributionType': 'DIRECT_ENTITLEMENT', 'imageAssetURL': ''}}],
                    'startAt': data['startAt'], 'endAt': data['endAt'], 'preconditionDrops': [],
                    'requiredMinutesWatched': 60*(number+1),
                    'self': {'dropInstanceID': None, 'isClaimed': False, 'currentMinutesWatched': 36}})
            if index == 0:
                for d in data['timeBasedDrops']:
                    d['self']['isClaimed'] = True
            else:
                data['_catalog_only'] = True
                data['self']['isAccountConnected'] = None
                for d in data['timeBasedDrops']:
                    d.pop('self', None)
            campaign = DropsCampaign(self, data, {})
            await self.gui.inv.add_campaign(campaign)
            if index == 0:
                self.gui.display_drop(next(iter(campaign.drops)))
                channel = Channel(self, id=42, login='offline_preview', display_name='离线演示频道')
                channel._stream = Stream(channel, id=1, game=data['game'], viewers=128, title='本地视觉测试')
                self.gui.channels.display(channel)
                self.gui.channels.set_watching(channel)
        self.gui.games = names[1:]
        self.gui.print('本地测试页面已准备；展示的是假数据，未访问 Twitch。')
        await self.gui.wait_until_closed()

    def close(self):
        pass

    async def shutdown(self):
        self._auth_state._logged_in.clear()

    def save(self, *, force=False):
        self.settings.save(force=force)


if __name__ == '__main__':
    service = MinerService(worker_factory=PreviewWorker, password='offline-preview-password')
    web.run_app(create_app(service, 'offline-preview-password'), host='127.0.0.1', port=int(os.environ.get('TDM_PREVIEW_PORT', '18744')),
                access_log=None)
