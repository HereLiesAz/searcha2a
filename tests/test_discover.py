import importlib.util
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
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

    def test_paginated_registry_fetches_original_agent_cards(self):
        # Registry metadata is never mistaken for a trusted Agent Card.
        original_url = 'https://weather.example.com/.well-known/agent-card.json'
        pages = []
        def get(url):
            pages.append(url)
            if url.startswith('https://registry.example.com/api/agents?'):
                return {'agents': [{'name': 'Untrusted directory row', 'wellKnownURI': original_url}]}
            if url == original_url:
                return CARD
            raise AssertionError('Unexpected URL: ' + url)

        result = discover.discover('weather', {
            'card_urls': [],
            'registry_urls': [],
            'paginated_registry_urls': ['https://registry.example.com/api/agents'],
        }, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['results'][0]['name'], 'Weather Expert')
        self.assertEqual(result['results'][0]['source_url'], original_url)
        self.assertEqual(result['results'][0]['verification'], 'card_read_only')
        self.assertEqual(pages[0], 'https://registry.example.com/api/agents?limit=30&offset=0&search=weather')
        self.assertEqual(pages[1], original_url)

    def test_paginated_registry_uses_bounded_offsets_and_ignores_next_links(self):
        first_page = [
            {'wellKnownURI': f'https://agent{i}.example.com/card.json'} for i in range(30)
        ]
        second_page = [{'wellKnownURI': 'https://weather.example.com/card.json'}]
        requested = []
        def get(url):
            requested.append(url)
            if url == 'https://registry.example.com/api/agents?limit=30&offset=0':
                return {'agents': first_page, 'next': 'https://private.example/agents'}
            if url == 'https://registry.example.com/api/agents?limit=30&offset=30':
                return {'agents': second_page}
            return CARD

        result = discover.discover('', {
            'card_urls': [],
            'registry_urls': [],
            'paginated_registry_urls': ['https://registry.example.com/api/agents'],
        }, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(result['count'], 1)  # Same endpoint deduplicates 31 cards.
        self.assertEqual([url for url in requested if '/api/agents?' in url], [
            'https://registry.example.com/api/agents?limit=30&offset=0',
            'https://registry.example.com/api/agents?limit=30&offset=30',
        ])
        self.assertNotIn('https://private.example/agents', requested)

    def test_live_search_uses_only_one_registry_page(self):
        cards = [{'wellKnownURI': f'https://agent{i}.example/card.json'} for i in range(30)]
        fetched = []
        def get(url):
            fetched.append(url)
            if '/api/agents?' in url:
                return {'agents': cards}
            return CARD

        discover.discover('weather', {
            'card_urls': [], 'registry_urls': [],
            'paginated_registry_urls': ['https://registry.example.com/api/agents'],
        }, fetch=get, github=lambda *_: ([], []))
        registry_requests = [url for url in fetched if '/api/agents?' in url]
        self.assertEqual(registry_requests, [
            'https://registry.example.com/api/agents?limit=30&offset=0&search=weather',
        ])

    def test_bad_paginated_registry_does_not_discard_seeded_cards(self):
        def get(url):
            if url == 'https://registry.example.com/api/agents?limit=30&offset=0&search=weather':
                raise discover.DiscoveryError('Unavailable')
            return CARD

        result = discover.discover('weather', {
            'card_urls': ['https://weather.example.com/card.json'],
            'registry_urls': [],
            'paginated_registry_urls': ['https://registry.example.com/api/agents'],
        }, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(result['count'], 1)
        self.assertIn('Registry unavailable: registry.example.com', result['warnings'])

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
