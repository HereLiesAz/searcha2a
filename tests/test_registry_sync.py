import importlib.util
import io
import json
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


    def test_more_than_150_cards_split_by_count_and_union_suppression(self):
        cards = [{**GOOD_CARD, 'id': format(i, '020x'),
                  'name': 'Agent ' + str(i)} for i in range(205)]
        requests = []
        def opener(request, timeout):
            payload = json.loads(request.data)
            requests.append(payload)
            ids = [item["id"] for item in payload["results"]]
            excluded = [i for i in ids if i in ("00000000000000000001", "00000000000000000063")]
            return Response(json.dumps({"status": "stored", "ingested": len(ids),
                "total": 205, "suppressed_ids": excluded}).encode())

        result = registry_sync.ingest({"query": "", "count": 205, "results": cards},
            "https://api.example", "private", opener=opener,
            sleep=lambda _: self.fail("unexpected retry"))
        self.assertEqual(len(requests), 3)
        self.assertEqual([len(req["results"]) for req in requests], [70, 70, 65])
        self.assertEqual(result["ingested"], 205)
        self.assertEqual(result["suppressed_ids"], [
            "00000000000000000001", "00000000000000000063"])
        self.assertEqual(registry_sync.filter_export_catalog(
            {"query": "", "results": cards}, result)["count"], 203)

    def test_large_valid_catalog_is_split_by_encoded_byte_length(self):
        cards = [{**GOOD_CARD, "id": format(i, "020x"),
                  "skills": [{"name": "s", "description": "x" * 500} for _ in range(40)]}
                 for i in range(70)]
        payloads = []
        def opener(request, timeout):
            payloads.append(request.data)
            batch = json.loads(request.data)["results"]
            return Response(json.dumps({"status": "stored", "ingested": len(batch),
                "total": 70, "suppressed_ids": []}).encode())
        result = registry_sync.ingest({"query": "", "results": cards},
            "https://api.example", "private", opener=opener,
            sleep=lambda _: self.fail("unexpected retry"))
        self.assertGreater(len(payloads), 1)
        self.assertTrue(all(len(raw) <= 1_000_000 for raw in payloads))
        self.assertEqual(sum(len(json.loads(raw)["results"]) for raw in payloads), 70)
        self.assertEqual(result["ingested"], 70)

    def test_oversized_single_card_fails_before_external_io(self):
        huge = {**GOOD_CARD, "description": "oversized" * 150000}
        def forbidden(*_args, **_kwargs):
            self.fail("An oversized card must not be uploaded")
        with self.assertRaisesRegex(ValueError, "One Agent Card"):
            registry_sync.ingest({"query": "", "results": [huge]},
                "https://api.example", "private", opener=forbidden)


    def test_evidence_export_is_authenticated_bounded_and_fails_soft(self):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            return Response(json.dumps({"entries": [
                {"id": "a" * 20, "verification_detail": {"level": 1}},
                {"id": "bad", "verification_detail": {"level": 1}},
            ]}).encode())
        result = registry_sync.fetch_evidence('https://api.example/', 'private', opener=opener)
        self.assertEqual(list(result), ["a" * 20])
        self.assertEqual(seen[0].full_url, 'https://api.example/internal/registry/evidence-export')
        self.assertEqual(seen[0].get_header('Authorization'), 'Bearer private')
        self.assertEqual(seen[0].get_method(), 'GET')

        def broken(request, timeout):
            raise urllib.error.URLError('offline')
        self.assertIsNone(registry_sync.fetch_evidence('https://api.example/', 'private', opener=broken))
        self.assertIsNone(registry_sync.fetch_evidence('http://insecure.example', 'private', opener=opener))
        self.assertIsNone(registry_sync.fetch_evidence('https://api.example/', '', opener=opener))


if __name__ == '__main__':
    unittest.main()
