"""Per-game monitoring quotas, independent of a channel's current broadcast game."""
from dataclasses import dataclass

PRIORITY_INTERVAL = 15 * 60
OTHER_INTERVAL = 60 * 60
PRIORITY_BUDGET = 100
OTHER_BUDGET = 80
OTHER_GAMES = 16


def search_due(last, now, priority):
    return last is None or now - last >= (PRIORITY_INTERVAL if priority else OTHER_INTERVAL)


def rotate_candidates(channels, offset, limit):
    channels = sorted({c.id: c for c in channels}.values(), key=lambda c: c.id)
    if not channels:
        return []
    offset %= len(channels)
    return (channels[offset:] + channels[:offset])[:limit]


def rank_candidates(channels, can_watch, *, small_acl=False):
    ranked = sorted({c.id: c for c in channels}.values(),
                    key=lambda c: (not can_watch(c), not c.online,
                                   not c.acl_based, -(c.viewers or 0), c.id))
    if small_acl:
        return ranked
    # Reserve two seats for offline specified channels, so a busy game does not
    # completely lose its opening notifications. The sample rotates on searches.
    offline = [c for c in ranked if c.acl_based and not c.online][:2]
    ids = {c.id for c in offline}
    return offline + [c for c in ranked if c.id not in ids]


@dataclass
class GamePool:
    game_id: int
    name: str
    priority: bool
    channels: list
    small_acl: bool = False


def allocate_channels(pools, maximum, watching=None):
    """100 priority / 80 other / spare; duplicates consume one websocket seat.

    Small specified lists may use spare capacity to reach ten even in the other
    pool. Large other lists retain the strict five-channel cap.
    """
    selected = {}
    assigned = {p.game_id: [] for p in pools}
    if watching is not None and maximum > 0:
        selected[watching.id] = watching
    for p in pools:
        if watching is not None and any(c.id == watching.id for c in p.channels):
            assigned[p.game_id].append(watching.id)

    def fill(pool, target, budget):
        spent = 0
        slots = assigned[pool.game_id]
        for channel in pool.channels:
            if len(slots) >= target:
                break
            if channel.id in slots:
                continue
            if channel.id not in selected:
                if spent >= budget or len(selected) >= maximum:
                    continue
                selected[channel.id] = channel
                spent += 1
            slots.append(channel.id)
        return spent

    priority = [p for p in pools if p.priority]
    other = [p for p in pools if not p.priority][:OTHER_GAMES]
    # A five-seat floor for every priority game before anybody expands to 20.
    budget = min(PRIORITY_BUDGET, maximum)
    for p in priority:
        budget -= fill(p, 5, budget)
    for p in priority:
        budget -= fill(p, 20, budget)
    budget = min(OTHER_BUDGET, maximum)
    for p in other:
        budget -= fill(p, 5, budget)
    # The current channel was reserved first, out of the flexible spare pool.
    for p in priority + other:
        if p.small_acl:
            fill(p, min(10, len(p.channels)), maximum)
    for p in priority:
        fill(p, 20, maximum)
    # Do not use spare seats to exceed a large other game's five-seat cap.
    return list(selected.values()), assigned
