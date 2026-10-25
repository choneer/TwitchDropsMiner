"""Offline regression coverage for incomplete SMARTBOX discovery and UI truthfulness."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_webui  # establishes isolated test data paths before core imports
from campaign_catalog import parse_catalog, merge_account_campaigns
from constants import ClientType
from inventory import DropsCampaign
from settings import Settings
from twitch import Twitch
from webui.bridge import WebBridge
from aiohttp import web
from aiohttp.test_utils import TestServer


def fixture(now):
    start, end = (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    campaign = {'id': 'public-1', 'name': 'Open campaign', 'status': 'ACTIVE',
        'startAt': start, 'endAt': end, 'game': {'id': '1', 'displayName': 'Game One'},
        'allow': {'isEnabled': False}, 'accountLinkURL': 'https://example.com/link',
        'self': {'isAccountConnected': True},  # untrusted: must not be imported
        'timeBasedDrops': [{'id': 'drop-1', 'name': 'Reward', 'startAt': start, 'endAt': end,
            'preconditionDrops': [], 'requiredMinutesWatched': 30,
            'self': {'isClaimed': True, 'currentMinutesWatched': 30, 'dropInstanceID': 'untrusted'},
            'benefitEdges': [{'benefit': {'id': 'reward-1', 'name': 'Reward',
                'distributionType': 'DIRECT_ENTITLEMENT', 'imageAssetURL': ''}}]}]}
    return {'lastUpdatedAt': now.isoformat(), 'refreshIntervalSeconds': 60,
            'data': [{'gameBoxArtURL': '', 'rewards': [campaign]}]}


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.payload = fixture(self.now)

    def test_catalog_is_discovery_only_not_account_state(self):
        cs, updated = parse_catalog(self.payload, self.now)
        c = cs['public-1']
        self.assertIsNone(c['self']['isAccountConnected'])
        self.assertNotIn('self', c['timeBasedDrops'][0])
        self.assertTrue(c['_catalog_only'])
        self.assertEqual(c['game']['name'], 'Game One')
        self.assertEqual(updated, self.now.isoformat())

    async def test_public_fetch_never_sends_account_credentials(self):
        from campaign_catalog import fetch_catalog
        observed = []
        async def catalog(request):
            observed.append(dict(request.headers))
            return web.json_response(self.payload)
        app = web.Application(); app.router.add_get('/catalog', catalog)
        async with TestServer(app) as server:
            with patch('campaign_catalog.CATALOG_URL', str(server.make_url('/catalog'))):
                campaigns, updated = await fetch_catalog()
        self.assertEqual(len(campaigns), 1)
        self.assertNotIn('Authorization', observed[0])
        self.assertNotIn('Cookie', observed[0])

    def test_reject_stale_future_and_missing_freshness(self):
        for age in (timedelta(minutes=-3), timedelta(minutes=6)):
            self.payload['lastUpdatedAt'] = (self.now-age).isoformat()
            self.payload['refreshIntervalSeconds'] = 99999999
            with self.assertRaises(ValueError): parse_catalog(self.payload, self.now)
        self.payload['lastUpdatedAt'] = None
        with self.assertRaises(ValueError): parse_catalog(self.payload, self.now)

    def test_invalid_expired_acl_and_cyclic_campaigns_are_not_mineable(self):
        for mutate in ('expired', 'acl', 'cycle', 'empty'):
            p = deepcopy(self.payload); c = p['data'][0]['rewards'][0]
            if mutate == 'expired': c['endAt'] = (self.now-timedelta(seconds=1)).isoformat()
            if mutate == 'acl': c['allow'] = {'isEnabled': True}
            if mutate == 'cycle': c['timeBasedDrops'][0]['preconditionDrops'] = [{'id': 'drop-1'}]
            if mutate == 'empty': c['timeBasedDrops'] = []
            self.assertEqual(parse_catalog(p, self.now)[0], {}, mutate)

    def test_subscription_reward_is_displayed_but_not_mined_or_marked_complete(self):
        c = self.payload['data'][0]['rewards'][0]
        c['timeBasedDrops'][0]['requiredSubs'] = 1
        c['timeBasedDrops'][0]['requiredMinutesWatched'] = 0
        public, updated = parse_catalog(self.payload, self.now)
        worker = SimpleNamespace(settings=SimpleNamespace(enable_badges_emotes=False))
        campaign = DropsCampaign(worker, public['public-1'], {})
        self.assertFalse(campaign.finished)
        self.assertFalse(campaign.can_earn())

    def test_twitch_account_state_overrides_public_data(self):
        public, updated = parse_catalog(self.payload, self.now)
        account = deepcopy(public)
        c = account['public-1']; c['self'] = {'isAccountConnected': False}
        c['allow']['channels'] = []
        c['timeBasedDrops'][0]['self'] = {'isClaimed': True, 'currentMinutesWatched': 30, 'dropInstanceID': None}
        merged = merge_account_campaigns(account, public)['public-1']
        self.assertFalse(merged['_catalog_only'])
        self.assertFalse(merged['self']['isAccountConnected'])
        self.assertTrue(merged['timeBasedDrops'][0]['self']['isClaimed'])
        self.assertIsNone(public['public-1']['self']['isAccountConnected'])

    async def test_empty_twitch_dashboard_is_supplemented_without_faking_progress(self):
        public, updated = parse_catalog(self.payload, self.now)
        service = SimpleNamespace(log=lambda *args: None)
        client = Twitch(Settings(SimpleNamespace(dump=False)), client_type=ClientType.SMARTBOX,
                        gui_factory=lambda c: WebBridge(c, service))
        client.gql_request = AsyncMock(side_effect=[
            {'data': {'currentUser': {'inventory': {'dropCampaignsInProgress': [], 'gameEventDrops': []}}}},
            {'data': {'currentUser': {'dropCampaigns': None}}}])
        try:
            with patch('campaign_catalog.fetch_catalog', new=AsyncMock(return_value=(public, updated))):
                await client.fetch_inventory()
            self.assertEqual(len(client.inventory), 1)
            state = client.gui.snapshot(); c = state['campaigns'][0]
            self.assertIsNone(c['linked'])
            self.assertFalse(c['eligible'])
            self.assertTrue(c['earnable'])  # may attempt; qualification is not verified
            self.assertFalse(c['drops'][0]['progress_known'])
            self.assertFalse(c['drops'][0]['claimed'])
            self.assertEqual(state['discovery']['source'], 'public_catalog')
        finally:
            if client._mnt_task:
                client._mnt_task.cancel()
                await asyncio.gather(client._mnt_task, return_exceptions=True)

    async def test_catalog_failure_is_visible_not_reported_as_no_campaigns(self):
        client = Twitch(Settings(SimpleNamespace(dump=False)), client_type=ClientType.SMARTBOX,
                        gui_factory=lambda c: WebBridge(c, SimpleNamespace(log=lambda *a: None)))
        client.gql_request = AsyncMock(side_effect=[
            {'data': {'currentUser': {'inventory': {'dropCampaignsInProgress': [], 'gameEventDrops': []}}}},
            {'data': {'currentUser': {'dropCampaigns': []}}}])
        try:
            with patch('campaign_catalog.fetch_catalog', new=AsyncMock(side_effect=ValueError('invalid fixture'))):
                await client.fetch_inventory()
            self.assertEqual(client.gui.snapshot()['discovery']['status'], 'error')
        finally:
            if client._mnt_task:
                client._mnt_task.cancel()
                await asyncio.gather(client._mnt_task, return_exceptions=True)

    def test_completed_inventory_is_not_an_unfinished_activity(self):
        public, updated = parse_catalog(self.payload, self.now)
        known = deepcopy(public)
        c = known['public-1']; c['self']['isAccountConnected'] = False
        c['timeBasedDrops'][0]['self'] = {'isClaimed': True, 'currentMinutesWatched': 30, 'dropInstanceID': None}
        data = merge_account_campaigns(known, public)['public-1']
        worker = SimpleNamespace(settings=SimpleNamespace(enable_badges_emotes=False),
                                 _auth_state=SimpleNamespace())
        bridge = WebBridge(worker, SimpleNamespace(log=lambda *a: None))
        campaign = DropsCampaign(worker, data, {})
        bridge.inv.campaigns[campaign.id] = campaign
        c = bridge.snapshot()['campaigns'][0]
        self.assertTrue(c['finished']); self.assertTrue(c['in_inventory'])
        self.assertFalse(c['earnable']); self.assertFalse(c['catalog_only'])
        self.assertTrue(c['drops'][0]['progress_known'])
