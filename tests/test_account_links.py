import unittest
from account_links import attach_link_evidence, link_key, reward_link_evidence


class AccountLinkTests(unittest.TestCase):
    def test_same_endpoint_matches_without_fabricating_campaign_state(self):
        campaigns = {'a': {'accountLinkURL': 'https://EXAMPLE.com/link/',
                           'self': {'isAccountConnected': None}}}
        count = attach_link_evidence(campaigns, [{'requiredAccountLink': 'https://example.com/link', 'isConnected': True}])
        self.assertEqual(count, 1)
        self.assertTrue(campaigns['a']['_account_link_evidence'])
        self.assertIsNone(campaigns['a']['self']['isAccountConnected'])

    def test_different_path_query_case_or_domain_remains_unknown(self):
        history = [{'requiredAccountLink': 'https://example.com/link?game=one', 'isConnected': True}]
        for url in ['https://example.com/link?game=two', 'https://other.example/link?game=one',
                    'https://example.com/Link?game=one', 'https://example.com/other?game=one']:
            c = {'a': {'accountLinkURL': url}}
            self.assertEqual(attach_link_evidence(c, history), 0)

    def test_false_is_evidence_but_missing_and_conflicting_are_unknown(self):
        url = 'https://example.com/link'
        self.assertEqual(reward_link_evidence([{'requiredAccountLink': url, 'isConnected': False}]), {url: False})
        self.assertEqual(reward_link_evidence([{'requiredAccountLink': url, 'isConnected': True},
                                               {'requiredAccountLink': url, 'isConnected': False}]), {})
        self.assertEqual(reward_link_evidence([{'requiredAccountLink': url},
                                               {'requiredAccountLink': url, 'isConnected': 'true'}]), {})

    def test_untrusted_catalog_evidence_is_cleared(self):
        c = {'a': {'accountLinkURL': 'https://example.com/link', '_account_link_evidence': True}}
        attach_link_evidence(c, [])
        self.assertNotIn('_account_link_evidence', c['a'])

    def test_invalid_or_generic_twitch_links_are_ignored(self):
        for url in [None, '', 'http://example.com/link', 'javascript:alert(1)',
                    'https://user:password@example.com/link', 'https://www.twitch.tv/', 'https://[broken']:
            self.assertIsNone(link_key(url))
