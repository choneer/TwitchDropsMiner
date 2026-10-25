"""Public discovery metadata only. Never send Twitch cookies or tokens here.

Compatibility source also used by upstream PR #1180. Account progress, linkage,
watching and claims remain Twitch-authoritative; catalog data has no self edge.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import aiohttp

CATALOG_URL = 'https://twitch-drops-api.sunkwi.com/v2/drops'
MAX_BYTES = 12 * 1024 * 1024


def merge_account_campaigns(account, public):
    def overlay(primary, secondary):
        result = deepcopy(secondary)
        for key, value in primary.items():
            result[key] = (overlay(value, result[key]) if isinstance(value, dict)
                           and isinstance(result.get(key), dict) else deepcopy(value))
        return result
    result = deepcopy(public)
    for key, campaign in account.items():
        result[key] = overlay(campaign, result.get(key, {}))
        result[key]['_catalog_only'] = False
    return result


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Invalid catalog timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Catalog timestamp has no timezone')
    return result


def parse_catalog(payload, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise ValueError('Invalid catalog structure')
    updated = timestamp(payload.get('lastUpdatedAt'))
    # Do not let an untrusted refresh interval extend the freshness boundary.
    if updated > now + timedelta(minutes=2) or now - updated > timedelta(minutes=5):
        raise ValueError('Catalog snapshot is stale')
    campaigns = {}
    for group in payload['data'][:1000]:
        if not isinstance(group, dict) or not isinstance(group.get('rewards'), list):
            continue
        for raw in group['rewards'][:1000]:
            try:
                c = deepcopy(raw)
                if not isinstance(c['id'], str) or not c['id'] or c['status'] not in ('ACTIVE', 'UPCOMING'):
                    continue
                start, end = timestamp(c['startAt']), timestamp(c['endAt'])
                if end <= now or start >= end:
                    continue
                c['startAt'] = start.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                c['endAt'] = end.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                game = c['game']
                if not isinstance(game, dict) or int(game['id']) <= 0:
                    continue
                game['name'] = game.get('displayName') or game.get('name')
                if not isinstance(game['name'], str) or not game['name'] or not isinstance(c['name'], str):
                    continue
                game['boxArtURL'] = group.get('gameBoxArtURL') or ''
                # Reject partial/invalid campaigns rather than dropping ACLs or prerequisites.
                allowed = c.get('allow') or {'isEnabled': False}
                if not isinstance(allowed, dict) or (allowed.get('isEnabled', True) and not allowed.get('channels')):
                    continue
                allowed.setdefault('channels', None)
                for channel in allowed['channels'] or []:
                    int(channel['id'])
                    assert isinstance(channel['name'], str) and channel['name']
                c['allow'] = allowed
                drops = c['timeBasedDrops']
                if not isinstance(drops, list) or not 1 <= len(drops) <= 100:
                    continue
                ids = {d['id'] for d in drops}
                if len(ids) != len(drops):
                    continue
                for d in drops:
                    assert isinstance(d['id'], str) and isinstance(d['name'], str)
                    assert type(d['requiredMinutesWatched']) is int and d['requiredMinutesWatched'] >= 0
                    drop_start, drop_end = timestamp(d['startAt']), timestamp(d['endAt'])
                    assert drop_start < drop_end
                    d['startAt'] = drop_start.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                    d['endAt'] = drop_end.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                    # Display subscription rewards, but never mine them as watch time.
                    assert type(d.get('requiredSubs', 0)) is int and d.get('requiredSubs', 0) >= 0
                    for pre in d.get('preconditionDrops') or []:
                        assert pre['id'] in ids and pre['id'] != d['id']
                    for edge in d['benefitEdges'] or []:
                        b = edge['benefit']
                        assert all(isinstance(b[k], str) for k in ('id', 'name', 'distributionType', 'imageAssetURL'))
                    d.setdefault('preconditionDrops', [])
                    d.pop('self', None)
                # Validate dependency cycles before the core recursively computes time.
                predecessors = {d['id']: [p['id'] for p in d['preconditionDrops'] or []] for d in drops}
                visiting, visited = set(), set()
                def visit(key):
                    if key in visiting:
                        raise ValueError('Cyclic catalog prerequisites')
                    if key in visited:
                        return
                    visiting.add(key)
                    for previous in predecessors[key]:
                        visit(previous)
                    visiting.remove(key)
                    visited.add(key)
                for key in predecessors:
                    visit(key)
                c['self'] = {'isAccountConnected': None}
                c['accountLinkURL'] = c.get('accountLinkURL') or ''
                c['_catalog_only'] = True
                campaigns[c['id']] = c
            except (KeyError, TypeError, ValueError, AssertionError, RecursionError):
                continue
    return campaigns, updated.isoformat()


async def fetch_catalog(proxy=None):
    # Separate session: Twitch authentication must never reach the public catalog.
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=20),
            headers={'Accept': 'application/json', 'User-Agent': 'DropHarbor public catalog'}) as session:
        async with session.get(CATALOG_URL, proxy=proxy or None, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f'Catalog HTTP {response.status}')
            content = bytearray()
            async for block in response.content.iter_chunked(65536):
                content.extend(block)
                if len(content) > MAX_BYTES:
                    raise ValueError('Catalog exceeds size limit')
            return parse_catalog(json.loads(content))
