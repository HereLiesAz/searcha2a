"""Public typed discovery and explicitly authorized private-feed publication."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover

CARD = {
    "name": "Weather Agent", "description": "Read-only weather metadata",
    "url": "https://weather.example.org/a2a",
    "skills": [{"name": "Forecast", "tags": ["weather"]}],
}
AI = "application/a2a-agent-card+json"
ERC_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"


class CatalogConnectorTests(unittest.TestCase):
    def test_ai_catalog_filters_non_a2a_entries_even_if_they_look_like_cards(self):
        catalog = {"specVersion": "1.0", "entries": [
            {"mediaType": AI, "data": CARD},
            {"mediaType": "application/mcp-server-card+json",
             "data": {**CARD, "name": "Not an A2A card"}},
            {"mediaType": "application/a2a+json", "url": "/other-agent"},
        ]}
        cards, urls = discover.unpack_cards(catalog, "https://catalog.example.org/ai", "catalog")
        self.assertEqual([c["name"] for c in cards], ["Weather Agent"])
        self.assertEqual(urls, ["https://catalog.example.org/other-agent"])

    def test_legacy_registry_entries_still_parse_without_media_types(self):
        feed = {"entries": [{"card": CARD}]}
        cards, urls = discover.unpack_cards(feed, "https://registry.example.org/feed.json",
                                            "registry")
        self.assertEqual([card["name"] for card in cards], ["Weather Agent"])
        self.assertEqual(urls, [])

    def test_inline_ai_catalog_nesting_is_bounded_and_ignores_untyped_branches(self):
        nested = {"specVersion": "1.0", "entries": [
            {"mediaType": AI, "data": CARD},
            {"mediaType": "application/ai-catalog+json", "data": {
                "entries": [{"mediaType": AI, "data": {**CARD, "name": "Too deep"}}]}},
        ]}
        root = {"specVersion": "1.0", "entries": [
            {"mediaType": "application/ai-catalog+json", "data": nested}]}
        cards, urls = discover.unpack_cards(root, "https://catalog.example.org/ai.json", "test")
        self.assertEqual([c["name"] for c in cards], ["Weather Agent"])
        self.assertEqual(urls, [])

    def test_typed_catalog_external_card_is_fetched_once_with_no_execution(self):
        root = "https://catalog.example.org/.well-known/ai-catalog.json"
        card_url = "https://agents.example.org/weather"
        requests = []
        documents = {
            root: {"specVersion": "1.0", "entries": [
                {"mediaType": AI, "url": card_url}]},
            card_url: CARD,
        }
        def get(url):
            requests.append(url)
            if url in documents:
                return documents[url]
            raise discover.DiscoveryError("No publisher proof")
        result = discover.discover("", {"card_urls": [root]}, fetch=get,
                                   github=lambda *_: ([], []))
        self.assertEqual(result["count"], 1)
        self.assertEqual(requests.count(card_url), 1)
        self.assertTrue(all("/a2a" not in url for url in requests))

    def test_erc8004_registration_points_to_only_the_a2a_card(self):
        registration = {"type": ERC_TYPE, "name": "ERC Registration",
                        "skills": [{"name": "Should not become a card"}],
                        "services": [
                            {"name": "MCP", "endpoint": "https://unsafe.example.org/mcp"},
                            {"name": "A2A", "endpoint": "https://agent.example.org/card"},
                            {"name": "web", "endpoint": "https://www.example.org"},
                        ]}
        cards, urls = discover.unpack_cards(registration, "https://chain.example.org/42", "erc8004")
        self.assertEqual(cards, [])
        self.assertEqual(urls, ["https://agent.example.org/card"])
        self.assertNotIn("verified", json.dumps(registration))

    def test_webfinger_requires_explicit_account_and_only_self_card_links(self):
        config = {
            "domain_urls": ["https://agents.example.org"],
            "webfinger_accounts": {"https://agents.example.org": [
                "alice", "bad@other", "../other", "alice@example", "bob.1"]},
        }
        sources = discover.domain_sources(config)
        urls = [u for u, _ in sources if "/.well-known/webfinger?" in u]
        self.assertEqual(urls, [
            "https://agents.example.org/.well-known/webfinger?resource=acct%3Aalice%40agents.example.org",
            "https://agents.example.org/.well-known/webfinger?resource=acct%3Abob.1%40agents.example.org",
        ])
        jrd = {"subject": "acct:alice@agents.example.org", "links": [
            {"rel": "self", "type": AI, "href": "https://agents.example.org/alice/card"},
            {"rel": "service", "type": AI, "href": "https://wrong.example.org/bad"},
            {"rel": "self", "type": "application/mcp-server-card+json",
             "href": "https://wrong.example.org/mcp"},
        ]}
        cards, refs = discover.unpack_cards(jrd, urls[0], "webfinger")
        self.assertEqual(cards, [])
        self.assertEqual(refs, ["https://agents.example.org/alice/card"])

    def test_private_catalog_defaults_to_no_publication(self):
        denied = {"url": "https://internal.example.org/catalog.json",
                  "auth_type": "bearer", "token": "private-token"}
        def fail(*_args, **_kwargs):
            raise AssertionError("A private feed without publish:true must not be fetched")
        result = discover.discover("", {"private_catalogs": [denied]}, fetch=fail,
                                   private_fetch=fail, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["warnings"], [])

    def test_authorized_catalog_is_explicit_and_never_republishes_raw_metadata(self):
        secret = "very-secret-private-token"
        catalog_url = "https://enterprise.example.org/private/catalog.json"
        card = {**CARD, "accessToken": secret}
        calls = []
        def private_get(url, scheme, token):
            calls.append((url, scheme, token))
            return {"agents": [{"card": card}]}
        result = discover.discover("", {
            "private_catalogs": [{
                "url": catalog_url, "auth_type": "api_key", "token": secret,
                "publish": True,
            }],
        }, fetch=lambda *_: [], private_fetch=private_get, github=lambda *_: ([], []))
        self.assertEqual(calls, [(catalog_url, "api_key", secret)])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["source"], "authorized-catalog")
        self.assertNotIn("raw_card", result["results"][0])
        self.assertNotIn(secret, json.dumps(result))

    def test_private_auth_is_never_forwarded_to_card_links_or_redirects(self):
        catalog_url = "https://enterprise.example.org/private/catalog.json"
        card_url = "https://external.example.org/card.json"
        public_requests = []
        private_requests = []
        def private_get(url, scheme, token):
            private_requests.append((url, scheme, token))
            return {"agents": [{"cardUrl": card_url}]}
        def get(url):
            public_requests.append(url)
            if url == card_url:
                return CARD
            raise discover.DiscoveryError("Proof unavailable")
        result = discover.discover("", {
            "private_catalogs": [{
                "url": catalog_url, "auth_type": "bearer",
                "token": "only-for-private-origin", "publish": True,
            }],
        }, fetch=get, private_fetch=private_get, github=lambda *_: ([], []))
        self.assertEqual(private_requests, [(catalog_url, "bearer", "only-for-private-origin")])
        self.assertIn(card_url, public_requests)
        self.assertEqual(result["count"], 1)
        self.assertNotIn("only-for-private-origin", json.dumps(result))
        self.assertIsNone(discover.NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://external.example.org/card.json"))

    def test_private_http_transport_supports_only_bearer_and_api_key(self):
        recorded = []
        payload = json.dumps({"agents": []}).encode("utf-8")
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        class Opener:
            def open(self, request, timeout):
                recorded.append(dict(request.header_items()))
                return Response(payload)
        with patch.object(discover, "require_public_https", return_value="https://example.org/card"), \
             patch.object(discover, "OPENER", Opener()):
            discover.fetch_json("https://example.org/card", private_auth=("bearer", "redacted"))
            discover.fetch_json("https://example.org/card", private_auth=("api_key", "redacted2"))
            with self.assertRaises(discover.DiscoveryError):
                discover.fetch_json("https://example.org/card", private_auth=("other", "bad"))
            with self.assertRaises(discover.DiscoveryError):
                discover.fetch_json("https://example.org/card", private_auth=("bearer", "bad\nheader"))
        self.assertEqual(recorded[0]["Authorization"], "Bearer redacted")
        self.assertEqual(recorded[1]["X-api-key"], "redacted2")
        self.assertNotIn("Authorization", recorded[1])

    def test_google_agent_registry_wrapper_only_accepts_typed_a2a_cards(self):
        feed = {"agents": [
            {"card": {"type": "A2A_AGENT_CARD", "content": CARD}},
            {"card": {"type": "MCP_SERVER_CARD", "content":
                      {**CARD, "name": "Fake MCP Agent"}}},
            {"card": {"type": "A2A_AGENT_CARD", "content": json.dumps(
                {**CARD, "name": "JSON Agent", "url": "https://json.example.org/a2a"})}},
        ]}
        records, refs = discover.unpack_cards(
            feed, "https://agentregistry.googleapis.com/v1/projects/p/locations/global/agents",
            "authorized-catalog")
        self.assertEqual([x["name"] for x in records], ["Weather Agent", "JSON Agent"])
        self.assertEqual(refs, [])

    def test_google_registry_lists_bounded_read_only_pages_with_source_privacy(self):
        source = "https://agentregistry.googleapis.com/v1/projects/team/locations/global/agents"
        visits = []
        def read(url, scheme, secret):
            visits.append((url, scheme, secret))
            if "pageToken=" not in url:
                return {"agents": [{"card": {"type": "A2A_AGENT_CARD", "content": CARD}}],
                        "nextPageToken": "private-cursor"}
            return {"agents": [{"card": {"type": "A2A_AGENT_CARD", "content":
                                        {**CARD, "name": "Research Agent",
                                         "url": "https://research.example.org/a2a"}}}]}
        result = discover.discover("", {
            "private_catalogs": [{
                "url": source, "kind": "gcp_agents_list",
                "auth_type": "bearer", "token": "private-access", "publish": True,
            }],
        }, private_fetch=read, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(visits), 2)
        self.assertEqual(visits[0], (source + "?pageSize=30", "bearer", "private-access"))
        self.assertEqual(visits[1][0], source + "?pageSize=30&pageToken=private-cursor")
        self.assertTrue(all(row["source_url"] == source for row in result["results"]))
        self.assertNotIn("private-cursor", json.dumps(result))
        self.assertNotIn("private-access", json.dumps(result))

    def test_google_registry_kind_rejects_unapproved_hosts_and_routes(self):
        calls = []
        def dangerous(*args):
            calls.append(args)
            raise AssertionError("Should not fetch Google-like API on arbitrary host")
        result = discover.discover("", {
            "private_catalogs": [{
                "url": "https://different.example.org/v1/projects/p/locations/global/agents",
                "kind": "gcp_agents_list", "auth_type": "bearer",
                "token": "private", "publish": True,
            }],
        }, private_fetch=dangerous, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(result["warnings"], ["Authorized catalog unavailable"])

    def test_private_error_never_exposes_source_hostname_or_credentials(self):
        source = {"url": "https://private-team.example.org/secret-feed.json",
                  "auth_type": "bearer", "token": "private-password", "publish": True}
        def unavailable(*_):
            raise discover.DiscoveryError("contains private-password")
        result = discover.discover("", {"private_catalogs": [source]},
                                   private_fetch=unavailable, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["warnings"], ["Authorized catalog unavailable"])
        self.assertNotIn("private-password", json.dumps(result))
        self.assertNotIn("private-team.example.org", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
