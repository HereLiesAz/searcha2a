"""Bounded public directory metadata searches without task execution or billing."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover


ENDPOINT = "https://allagents.app/discover"
CARD_URL = "https://forecast.example.org/.well-known/agent-card.json"
CARD = {
    "name": "Forecast Agent", "description": "Weather",
    "url": "https://forecast.example.org/a2a",
    "skills": [{"id": "forecast", "name": "Forecast", "tags": ["weather"]}],
}


class PublicDirectoryDiscoveryTests(unittest.TestCase):
    def test_absent_and_disabled_sources_do_not_issue_post(self):
        def fail(*_):
            raise AssertionError("No public directory call permitted")
        self.assertEqual(discover.public_directory_sources({}, post=fail), ([], [], []))
        self.assertEqual(discover.public_directory_sources({
            "public_directory_apis": [{"endpoint": ENDPOINT, "enabled": False}]},
            post=fail), ([], [], []))

    def test_schedule_rotates_three_fixed_search_topics(self):
        topics = []
        def search(url, need):
            self.assertEqual(url, ENDPOINT)
            topics.append(need)
            return {"results": []}
        config = {"rotation_index": 0, "public_directory_apis": [
            {"endpoint": ENDPOINT, "enabled": True}]}
        self.assertEqual(discover.public_directory_sources(config, post=search),
                         ([], [], []))
        self.assertEqual(topics, ["research", "coding", "legal"])
        config["rotation_index"] = 1
        discover.public_directory_sources(config, post=search)
        self.assertEqual(topics[3:], ["automation", "memory", "identity"])

    def test_collects_card_links_but_never_execution_urls(self):
        def search(_url, _need):
            return {"agents": [
                {"name": "Forecast", "agent_card": CARD_URL,
                 "endpoints": {"a2a": "https://untrusted.example.org/a2a"}},
                {"name": "No pointer", "url": "https://untrusted.example.org/task"},
                {"description": CARD_URL, "action_url": CARD_URL},
                {"cardUrl": "http://localhost/private.json"},
                {"agentCardUrl": "https://username:pass@example.org/card.json"},
            ]}
        cards, urls, warnings = discover.public_directory_sources({
            "rotation_index": 0,
            "public_directory_apis": [{"endpoint": ENDPOINT, "enabled": True}]},
            post=search)
        self.assertEqual(cards, [])
        self.assertEqual(urls, [CARD_URL])
        self.assertEqual(warnings, [])

    def test_embedded_directory_card_requires_publisher_origin_fetch(self):
        def search(_url, _topic):
            return {"matches": [
                {"card": CARD},
                {"card": {**CARD, "name": "No endpoint", "url": ""}},
                {"card": {"name": "Not an A2A card"}},
            ]}
        cards, urls, warnings = discover.public_directory_sources({
            "rotation_index": 0,
            "public_directory_apis": [{"endpoint": ENDPOINT, "enabled": True}]},
            post=search)
        self.assertEqual(cards, [])
        self.assertEqual(urls, [CARD_URL])
        self.assertEqual(warnings, [])

    def test_declared_a2a_endpoint_probes_canonical_card_not_execution(self):
        def search(_url, _topic):
            return {"results": [
                {"endpoints": {"a2a": "https://other.example.org/a2a"}},
                {"reach": {"type": "a2a", "door": "https://third.example.org/gateway"}},
                {"reach": {"type": "mcp", "door": "https://mcp.example.org/gateway"}},
                {"endpoints": {"a2a": "http://localhost:8080/a2a"}},
                {"endpoints": {"a2a": "https://user:password@example.org/a2a"}},
            ]}
        cards, urls, warnings = discover.public_directory_sources({
            "rotation_index": 0,
            "public_directory_apis": [{"endpoint": ENDPOINT, "enabled": True}]},
            post=search)
        self.assertEqual(cards, [])
        self.assertEqual(urls, [
            "https://other.example.org/.well-known/agent-card.json",
            "https://third.example.org/.well-known/agent-card.json",
        ])
        self.assertEqual(warnings, [])

    def test_candidates_are_crawled_by_existing_public_https_get_only(self):
        fetched = []
        posted = []
        def post(url, topic):
            posted.append((url, topic))
            return {"items": [{"agentCardUrl": CARD_URL}]}
        def get(url):
            fetched.append(url)
            if url == CARD_URL:
                return CARD
            raise discover.DiscoveryError("No proof")
        result = discover.discover("", {
            "rotation_index": 0,
            "public_directory_apis": [{"endpoint": ENDPOINT, "enabled": True}],
        }, public_discover=post, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["source_url"], CARD_URL)
        self.assertEqual(result["results"][0]["source"], "public-directory")
        self.assertEqual(fetched, [CARD_URL])
        self.assertEqual(posted, [(ENDPOINT, "research"), (ENDPOINT, "coding"),
                                  (ENDPOINT, "legal")])
        self.assertNotIn("identity_verified", result["results"][0])

    def test_publisher_origin_card_wins_over_stale_directory_embed(self):
        posted = []
        fetched = []
        stale = {**CARD, "name": "Stale Directory Entry"}
        def post(_url, topic):
            posted.append(topic)
            return {"results": [{"card": stale}]}
        def get(url):
            fetched.append(url)
            if url == CARD_URL:
                return CARD
            raise discover.DiscoveryError("No proof")
        result = discover.discover("", {
            "rotation_index": 0,
            "card_urls": [CARD_URL],
            "public_directory_apis": [{"endpoint": ENDPOINT, "enabled": True}],
        }, public_discover=post, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["name"], "Forecast Agent")
        self.assertEqual(result["results"][0]["source_url"], CARD_URL)
        self.assertEqual(result["results"][0]["source"], "seed")
        self.assertEqual(fetched, [CARD_URL])
        self.assertEqual(posted, ["research", "coding", "legal"])

    def test_nested_directory_json_isolated_from_remaining_sources(self):
        nested = {}
        for _ in range(1200):
            nested = {"data": nested}
        def post(_url, topic):
            if topic == "research":
                return nested
            return {"agents": [{"card_url": CARD_URL}]}
        cards, links, warnings = discover.public_directory_sources({
            "rotation_index": 0,
            "public_directory_apis": [{"endpoint": ENDPOINT, "enabled": True}],
        }, post=post)
        self.assertEqual(cards, [])
        self.assertEqual(links, [CARD_URL])
        self.assertEqual(warnings, ["Public directory metadata unavailable"])

    def test_live_search_never_starts_external_directory_search(self):
        def never(*_):
            raise AssertionError("Live query must not POST to directory")
        result = discover.discover("weather", {
            "public_directory_apis": [{"endpoint": ENDPOINT, "enabled": True}]},
            public_discover=never, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)

    def test_only_allowlisted_exact_discovery_endpoint_is_accepted(self):
        invalid = [
            "https://allagents.app/relay", "https://allagents.app/a2a",
            "https://allagents.app/register", "https://allagents.app/discover?x=1",
            "https://attacker.example.org/discover",
        ]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                def forbidden(*_):
                    raise AssertionError("Unsupported path must not be contacted")
                cards, urls, warnings = discover.public_directory_sources({
                    "public_directory_apis": [
                        {"endpoint": endpoint, "enabled": True}]}, post=forbidden)
                self.assertEqual((cards, urls), ([], []))
                self.assertEqual(warnings, ["Public directory source unsupported"])

    def test_transport_sends_only_public_nonpersonal_metadata_body(self):
        requests = []
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return Response(b'{"agents":[]}')
        with patch.object(discover, "require_public_https", return_value=ENDPOINT), \
             patch.object(discover, "OPENER", Opener()):
            actual = discover.post_public_directory_discover(ENDPOINT, "research")
            with self.assertRaises(discover.DiscoveryError):
                discover.post_public_directory_discover(ENDPOINT, "user secrets")
            with self.assertRaises(discover.DiscoveryError):
                discover.post_public_directory_discover("https://allagents.app/relay",
                                                         "research")
        self.assertEqual(actual, {"agents": []})
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "need": "research", "alive": True, "protocols": ["A2A"],
            "endpoint": "a2a", "limit": 25,
        })
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(discover.NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://example.org/other"))

    def test_non_json_fails_closed_and_does_not_publish_errors(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/html"}
        class Opener:
            def open(self, *_args, **_kwargs):
                return Response(b"<html></html>")
        with patch.object(discover, "require_public_https", return_value=ENDPOINT), \
             patch.object(discover, "OPENER", Opener()), \
             self.assertRaises(discover.DiscoveryError):
            discover.post_public_directory_discover(ENDPOINT, "research")

    def test_provider_failure_does_not_block_other_search_terms(self):
        calls = []
        def search(_url, topic):
            calls.append(topic)
            if topic == "research":
                raise discover.DiscoveryError("Private provider traceback")
            return {"results": [{"cardUrl": CARD_URL}]}
        cards, urls, warnings = discover.public_directory_sources({
            "rotation_index": 0, "public_directory_apis": [
                {"endpoint": ENDPOINT, "enabled": True}]}, post=search)
        self.assertEqual(cards, [])
        self.assertEqual(urls, [CARD_URL])
        self.assertEqual(warnings, ["Public directory metadata unavailable"])
        self.assertEqual(calls, ["research", "coding", "legal"])


if __name__ == "__main__":
    unittest.main()
