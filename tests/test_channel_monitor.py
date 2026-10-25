"""Offline quotas, discovery cadence, subscriptions and priority switching."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
import unittest

from channel_monitor import GamePool, allocate_channels, rotate_candidates, rank_candidates, search_due


def channels(start, count, online=True):
    return [SimpleNamespace(id=i, online=online, acl_based=True, viewers=100 if online else None)
            for i in range(start, start+count)]


class QuotaTests(unittest.TestCase):
    def test_five_priority_games_and_sixteen_other_games(self):
        pools = [GamePool(i, f'Priority {i}', True, channels(i*1000, 500)) for i in range(5)]
        pools += [GamePool(100+i, f'Other {i}', False, channels(100000+i*1000, 500)) for i in range(17)]
        selected, assigned = allocate_channels(pools, 199)
        self.assertEqual(len(selected), 180)
        self.assertTrue(all(len(assigned[i]) == 20 for i in range(5)))
        self.assertTrue(all(len(assigned[100+i]) == 5 for i in range(16)))
        self.assertEqual(assigned[116], [])

    def test_warthunder_five_offline_survive_thousands_of_online_candidates(self):
        wt = channels(884000, 5, online=False)
        pools = [GamePool(1, 'War Thunder', True, wt, True),
                 GamePool(2, 'Battlefield', True, channels(1, 4000))]
        selected, assigned = allocate_channels(pools, 199)
        self.assertEqual(set(assigned[1]), {c.id for c in wt})
        self.assertEqual(len(assigned[2]), 20)
        self.assertLessEqual(len(selected), 199)

    def test_floor_before_expansion_and_never_exceed_capacity(self):
        pools = [GamePool(i, str(i), True, channels(i*1000, 500)) for i in range(30)]
        selected, assigned = allocate_channels(pools, 199)
        self.assertTrue(all(len(assigned[i]) >= 5 for i in range(20)))
        self.assertTrue(all(len(v) <= 20 for v in assigned.values()))
        self.assertLessEqual(len(selected), 199)

    def test_current_channel_reserved_and_shared_channel_deduplicated(self):
        shared = channels(1, 5)
        watching = channels(99999, 1)[0]
        selected, assigned = allocate_channels([GamePool(1, 'A', True, shared),
            GamePool(2, 'B', False, shared)], 6, watching)
        self.assertEqual(len(selected), 6)
        self.assertIn(watching, selected)
        self.assertEqual(assigned[1], assigned[2])

    def test_dynamic_seats_complete_small_lists_without_inflating_large_other_games(self):
        pools = [GamePool(i, str(i), False, channels(i*1000, 8), True) for i in range(16)]
        pools += [GamePool(50, 'Not in first 16', False, channels(90000, 100))]
        selected, assigned = allocate_channels(pools, 199)
        self.assertTrue(all(len(assigned[i]) == 8 for i in range(16)))
        self.assertEqual(assigned[50], [])
        self.assertEqual(len(selected), 128)

    def test_rotating_large_acl_sample_and_offline_seats(self):
        pool = channels(1, 100)
        self.assertNotEqual([c.id for c in rotate_candidates(pool, 0, 20)],
                            [c.id for c in rotate_candidates(pool, 20, 20)])
        ranked = rank_candidates(pool + channels(1000, 5, False), lambda c: c.online)
        self.assertFalse(ranked[0].online)
        self.assertFalse(ranked[1].online)
        self.assertTrue(ranked[2].online)

    def test_exact_fifteen_minute_and_hour_boundaries(self):
        self.assertTrue(search_due(None, 0, True))
        self.assertFalse(search_due(0, 899, True))
        self.assertTrue(search_due(0, 900, True))
        self.assertFalse(search_due(0, 3599, False))
        self.assertTrue(search_due(0, 3600, False))


import test_webui  # isolated paths before core imports
from twitch import Twitch
from settings import Settings
from channel import Channel
from constants import ClientType
from constants import State
from utils import Game
from webui.bridge import WebBridge


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_dispatches_three_priority_ticks_then_hourly_inventory(self):
        client = self.make_client()
        base = datetime(2026, 10, 25, tzinfo=timezone.utc)
        elapsed, events = [0], []
        class Clock:
            @staticmethod
            def now(tz=None):
                return base + timedelta(seconds=elapsed[0])
        async def sleep(seconds):
            elapsed[0] += seconds
        def change(state):
            events.append((elapsed[0], state, client._monitor_force_priority, client._monitor_force_all))
            client._monitor_force_priority = client._monitor_force_all = False
        client.change_state = change
        with patch('twitch.datetime', Clock), patch('twitch.asyncio.sleep', side_effect=sleep):
            await client._maintenance_task.__wrapped__(client)
        self.assertEqual([e[0] for e in events], [900, 1800, 2700, 3600])
        self.assertTrue(all(e[1] is State.CHANNELS_CLEANUP and e[2] and not e[3] for e in events[:3]))
        self.assertIs(events[-1][1], State.INVENTORY_FETCH)
        self.assertTrue(events[-1][3])

    def make_client(self):
        c = Twitch(Settings(SimpleNamespace(dump=False)), client_type=ClientType.SMARTBOX,
                   gui_factory=lambda t: WebBridge(t, SimpleNamespace(log=lambda *a: None)))
        c.settings.priority = ['War Thunder', 'Battlefield']
        c.websocket = SimpleNamespace(remove_topics=Mock(), add_topics=Mock())
        c.can_watch = lambda ch: ch.online and ch.game in c.wanted_games
        c.bulk_check_online = AsyncMock()
        c.get_live_streams = AsyncMock(return_value=[])
        return c

    def add_game(self, client, game_id, name, first, count, hours=1):
        game = Game({'id': str(game_id), 'name': name})
        acl = [Channel(client, id=i, login=f'channel{i}', acl_based=True) for i in range(first, first+count)]
        client.wanted_games.append(game)
        client.inventory.append(SimpleNamespace(game=game, allowed_channels=acl,
            can_earn_within=lambda t: True, ends_at=datetime.now(timezone.utc)+timedelta(hours=hours)))
        return game, acl

    async def test_retains_offline_game_affinity_cache_and_switches_on_open(self):
        client = self.make_client()
        wt, wt_channels = self.add_game(client, 1, 'War Thunder', 1, 5)
        bf, bf_channels = self.add_game(client, 2, 'Battlefield', 100, 1000)
        other, _ = self.add_game(client, 3, 'Other', 10000, 9)
        live = bf_channels[0]
        live._stream = SimpleNamespace(game=bf, viewers=100, drops_enabled=True)
        client.channels[live.id] = live
        client.watching_channel.set(live)
        with patch('twitch.time', return_value=1000):
            await client._refresh_monitored_channels()
        self.assertTrue(all(ch.id in client.channels for ch in wt_channels))
        self.assertIs(client.watching_channel.get_with_default(None), live)
        self.assertEqual(len(client.websocket.add_topics.call_args.args[0]), len(client.channels)*2)
        self.assertLessEqual(max(len(call.args[0]) for call in client.bulk_check_online.await_args_list), 40)
        for ch in wt_channels:
            self.assertEqual(ch.monitor_games, ['War Thunder'])
        calls = client.bulk_check_online.await_count
        with patch('twitch.time', return_value=1901):
            await client._refresh_monitored_channels()
        self.assertEqual(client.bulk_check_online.await_count - calls, 2)
        calls = client.bulk_check_online.await_count
        with patch('twitch.time', return_value=4601):
            await client._refresh_monitored_channels()
        self.assertEqual(client.bulk_check_online.await_count - calls, 3)
        # The actual existing opening-event handler must switch BF -> War Thunder.
        target = client.channels[wt_channels[0].id]
        target._stream = SimpleNamespace(game=wt, viewers=10, drops_enabled=True)
        client.on_channel_update(target, None, target._stream)
        self.assertIs(client.watching_channel.get_with_default(None), target)

    async def test_top_sixteen_other_games_sorted_by_deadline(self):
        client = self.make_client()
        client.settings.priority = []
        for i in range(20):
            self.add_game(client, i, str(i), i*100+1, 5, hours=20-i)
        await client._refresh_monitored_channels()
        self.assertEqual([g['game'] for g in client.monitoring['games']], [str(i) for i in range(19, 3, -1)])
        self.assertEqual(client.bulk_check_online.await_count, 16)
        self.assertEqual(len(client.channels), 80)

    async def test_failed_search_keeps_existing_candidates(self):
        client = self.make_client()
        self.add_game(client, 1, 'War Thunder', 1, 5)
        with patch('twitch.time', return_value=0):
            await client._refresh_monitored_channels()
        before = set(client.channels)
        client.bulk_check_online.side_effect = TimeoutError()
        with patch('twitch.time', return_value=901):
            await client._refresh_monitored_channels()
        self.assertEqual(set(client.channels), before)

    async def test_timer_tick_forces_only_priority_search_and_hour_forces_both(self):
        client = self.make_client()
        self.add_game(client, 1, 'War Thunder', 1, 5)
        self.add_game(client, 2, 'Other', 10, 5)
        with patch('twitch.time', return_value=100):
            await client._refresh_monitored_channels()
        client._monitor_force_priority = True
        before = client.bulk_check_online.await_count
        with patch('twitch.time', return_value=999):
            await client._refresh_monitored_channels()
        self.assertEqual(client.bulk_check_online.await_count-before, 1)
        client._monitor_force_all = True
        before = client.bulk_check_online.await_count
        with patch('twitch.time', return_value=3700):
            await client._refresh_monitored_channels()
        self.assertEqual(client.bulk_check_online.await_count-before, 2)
