import importlib.util
import io
from pathlib import Path
import unittest
import urllib.error

spec = importlib.util.spec_from_file_location('registry_sync', Path(__file__).parents[1] / 'src/registry_sync.py')
registry_sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_sync)

GOOD_CARD = {
    'id': 'aaaaaaaaaaaaaaaaaaaa',
    'name': 'Weather Agent',
    'description': 'Weather reports',
    'source_url': 'https://agents.example.com/card.json',
    'endpoint': 'https://agents.example.com/a2a',
    'skills': [{'name': 'Weather'}],
    'verification': 'card_read_only',
}


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class RegistrySyncTests(unittest.TestCase):
    def test_successful_authenticated_crawl_upload(self):
        requests = []
        def opener(request, timeout):
            requests.append((request, timeout))
            return Response(b'{"status":"stored","ingested":1,"total":7,"suppressed_ids":[]}')

        result = registry_sync.ingest(
            {'query': '', 'results': [GOOD_CARD]}, 'https://api.example/', 'private',
            opener=opener, sleep=lambda _: self.fail('unexpected retry'))
        self.assertEqual(result, {'status': 'stored', 'ingested': 1, 'total': 7, 'suppressed_ids': []})
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(timeout, 15)
        self.assertEqual(request.full_url, 'https://api.example/internal/registry/ingest')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private')
        self.assertIn(b'Weather Agent', request.data)

    def test_temporary_gateway_failure_retries_without_losing_payload(self):
        requests = []
        waits = []
        def opener(request, timeout):
            requests.append(request.data)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 503, 'Unavailable', {}, None)
            return Response(b'{"status":"stored","ingested":1,"total":1,"suppressed_ids":[]}')

        result = registry_sync.ingest(
            {'query': '', 'results': [GOOD_CARD]}, 'https://api.example', 'private',
            attempts=2, opener=opener, sleep=waits.append)
        self.assertEqual(result['total'], 1)
        self.assertEqual(waits, [4])
        self.assertEqual(requests[0], requests[1])

    def test_curator_rejection_filters_static_catalog_and_azphalt_export(self):
        published = {
            'query': '', 'count': 2,
            'results': [GOOD_CARD, {**GOOD_CARD, 'id': 'bbbbbbbbbbbbbbbbbbbb', 'name': 'Rejected'}],
            'warnings': ['Registry timeout'], 'generated_at': '2026-11-12T00:00:00Z',
        }
        output = registry_sync.filter_export_catalog(
            published, {'status': 'stored', 'suppressed_ids': ['bbbbbbbbbbbbbbbbbbbb']})
        self.assertEqual(output['count'], 1)
        self.assertEqual(output['results'][0]['id'], 'aaaaaaaaaaaaaaaaaaaa')
        self.assertEqual(output['warnings'], ['Registry timeout'])
        self.assertEqual(published['count'], 2)  # Original crawler findings remain unchanged.

    def test_publishing_fails_closed_if_registry_suppression_manifest_missing(self):
        catalog = {'query': '', 'results': [GOOD_CARD]}
        with self.assertRaisesRegex(ValueError, 'suppression manifest'):
            registry_sync.filter_export_catalog(catalog, {'status': 'stored'})
        with self.assertRaisesRegex(ValueError, 'suppression manifest'):
            registry_sync.filter_export_catalog(catalog, {'suppressed_ids': ['NOT_AN_ID']})

    def test_rejected_secret_does_not_retry(self):
        calls = []
        def opener(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 401, 'Denied', {}, None)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
            registry_sync.ingest(
                {'query': '', 'results': [GOOD_CARD]}, 'https://api.example', 'invalid',
                opener=opener, sleep=lambda _: self.fail('should not retry'))
        self.assertEqual(len(calls), 1)

    def test_interactive_or_oversize_catalog_rejected_before_network(self):
        def opener(*_args, **_kwargs):
            self.fail('unexpected HTTP call')
        with self.assertRaisesRegex(ValueError, 'scheduled'):
            registry_sync.ingest(
                {'query': 'weather', 'results': [GOOD_CARD]},
                'https://api.example', 'private', opener=opener)
        with self.assertRaisesRegex(ValueError, '70'):
            registry_sync.ingest(
                {'query': '', 'results': [GOOD_CARD] * 71},
                'https://api.example', 'private', opener=opener)


if __name__ == '__main__':
    unittest.main()
