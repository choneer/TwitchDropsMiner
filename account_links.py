"""Evidence from Twitch reward history, distinct from campaign eligibility."""
from urllib.parse import urlsplit, urlunsplit


def link_key(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            return None
        # A Twitch landing page is not a publisher account connection endpoint.
        if url.hostname.lower() == 'twitch.tv' or url.hostname.lower().endswith('.twitch.tv'):
            return None
        # Do not merge unrelated paths or query parameters on a publisher domain.
        return urlunsplit(('https', url.netloc.lower(), url.path.rstrip('/'), url.query, ''))
    except ValueError:
        return None


def reward_link_evidence(history):
    states = {}
    for entry in history or []:
        if not isinstance(entry, dict) or type(entry.get('isConnected')) is not bool:
            continue
        key = link_key(entry.get('requiredAccountLink'))
        if key:
            states.setdefault(key, set()).add(entry['isConnected'])
    # Conflicting evidence must remain unknown, not resolved with a truthy OR.
    return {key: next(iter(values)) for key, values in states.items() if len(values) == 1}


def attach_link_evidence(campaigns, history):
    states = reward_link_evidence(history)
    matched = 0
    for campaign in campaigns.values():
        campaign.pop('_account_link_evidence', None)
        connected = states.get(link_key(campaign.get('accountLinkURL')))
        if type(connected) is bool:
            campaign['_account_link_evidence'] = connected
            matched += 1
    return matched
