import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('discover', Path(__file__).parents[1] / 'src/discover.py')
discover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discover)

CARD = {
    'name': 'Weather Expert',
    'description': 'Reports clouds and precipitation',
    'url': 'https://weather.example.com/a2a',
    'skills': [{'name': 'Forecast', 'description': 'Predicts tomorrow weather', 'tags': ['weather']}],
    'capabilities': {'streaming': True},
}


class DiscoverTests(unittest.TestCase):
    def test_normalize_agent_card(self):
        result = discover.normalize_card(CARD, 'https://example.com/card.json', 'seed')
        self.assertEqual(result['name'], 'Weather Expert')
        self.assertEqual(result['skills'][0]['name'], 'Forecast')
        self.assertEqual(result['verification'], 'card_read_only')
        self.assertEqual(result['endpoint'], 'https://weather.example.com/a2a')

    def test_reject_non_agent_json(self):
        self.assertIsNone(discover.normalize_card({'name': 'Not an agent'}, 'https://example.com', 'seed'))
        self.assertIsNone(discover.normalize_card({'skills': [{'name': 'A'}]}, 'https://example.com', 'seed'))

    def test_rank_and_deduplication(self):
        other = {**CARD, 'name': 'Calendar Agent', 'url': 'https://calendar.example.com/a2a',
                 'skills': [{'name': 'Timekeeping', 'description': 'Meeting coordination'}]}
        sources = {'card_urls': ['https://example.com/a', 'https://example.com/b', 'https://example.com/c'],
                   'registry_urls': []}
        docs = {'https://example.com/a': CARD, 'https://example.com/b': CARD,
                'https://example.com/c': other}
        result = discover.discover('weather', sources, fetch=lambda url: docs[url],
                                   github=lambda *_: ([], []))
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['results'][0]['name'], 'Weather Expert')

    def test_registry_embedded_card(self):
        config = {'card_urls': [], 'registry_urls': ['https://registry.example.com/agents']}
        result = discover.discover('forecast', config,
            fetch=lambda url: {'agents': [{'card': CARD}]}, github=lambda *_: ([], []))
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['results'][0]['source'], 'registry')

    def test_github_code_search_builds_raw_url_when_download_url_missing(self):
        fake_items = {'items': [{'path': '.well-known/agent-card.json',
                     'repository': {'full_name': 'octo/agents', 'default_branch': 'main'}}]}
        with patch.object(discover, 'fetch_json', return_value=fake_items):
            sources, errors = discover.github_sources('weather', 'example-token')
        self.assertEqual(errors, [])
        self.assertEqual(sources[0][0],
                         'https://raw.githubusercontent.com/octo/agents/main/.well-known/agent-card.json')

    def test_private_host_rejected(self):
        for url in ['http://example.com', 'https://localhost/x', 'https://127.0.0.1/a',
                    'https://user:pass@example.com/x', 'file:///etc/passwd', 'https://example.com:8443/x']:
            with self.subTest(url=url), self.assertRaises(discover.DiscoveryError):
                discover.require_public_https(url)

    def test_redirect_disabled(self):
        self.assertIsNone(discover.NoRedirect().redirect_request(None, None, 302, '', {},
                                                                  'http://localhost/private'))

    def test_missing_sources_stays_empty_not_fabricated(self):
        result = discover.discover('weather', {'card_urls': [], 'registry_urls': []},
                                   github=lambda *_: ([], ['search unavailable']))
        self.assertEqual(result['results'], [])
        self.assertEqual(result['warnings'], ['search unavailable'])


if __name__ == '__main__':
    unittest.main()
