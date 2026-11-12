"""Adversarial coverage for bounded, read-only multi-tier public discovery."""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover


CARD = {
    "name": "Weather Agent",
    "description": "Public weather information",
    "url": "https://weather.example.com/a2a",
    "skills": [{"id": "forecast", "name": "Forecast", "tags": ["weather"]}],
}


class CrawlerTargetsTests(unittest.TestCase):
    def test_domain_paths_cover_canonical_legacy_and_manifests(self):
        found = discover.domain_sources({"domain_urls": ["https://agents.example.com"]})
        self.assertEqual([url for url, _ in found], [
            "https://agents.example.com/.well-known/agent-card.json",
            "https://agents.example.com/.well-known/agent.json",
            "https://agents.example.com/.well-known/agents.json",
            "https://agents.example.com/agents.json",
            "https://agents.example.com/agent.json",
            "https://agents.example.com/a2a/.well-known/agent.json",
            "https://agents.example.com/.wellknown/agent-card.json",
        ])

    def test_no_arbitrary_domain_paths_ports_or_assistant_enumeration(self):
        config = {"domain_urls": ["https://agents.example.com", "https://agents.example.com/private",
                    "http://insecure.example", "https://agents.example.com:8443"],
                  "assistant_ids": {"https://agents.example.com": ["known-1", "bad/id", ""]}}
        found = [url for url, _ in discover.domain_sources(config)]
        self.assertEqual(len(found), 8)
        self.assertEqual(found[-1],
                         "https://agents.example.com/.well-known/agent-card.json?assistant_id=known-1")
        self.assertTrue(all(url.startswith("https://agents.example.com/") for url in found))

    def test_references_support_custom_card_paths_without_invoking_endpoints(self):
        row = {"wellKnownURI": "https://public.example/card", "cardUrl": "/agent-card.json",
               "protocol": "a2a", "url": "https://public.example/a2a",
               "description": "https://private.example/evil.json"}
        self.assertEqual(discover.card_references(row, "https://public.example/registry"), [
            "https://public.example/card", "https://public.example/agent-card.json",
        ])
        self.assertEqual(discover.card_references({"cardUrl": "http://localhost/secret.json"},
                                                  "https://public.example/"), [])
        self.assertEqual(discover.card_references({"cardUrl": "https://user:pass@host.test/card.json"},
                                                  "https://public.example/"), [])

    def test_embedded_multiagent_manifest_and_card_pointers(self):
        doc = {"agents": [
            {"card": CARD},
            {"card": {**CARD, "name": "Coding Agent", "url": "https://code.example.com/a2a"}},
            {"cardUrl": "https://another.example.com/.well-known/agent-card.json"},
        ]}
        cards, refs = discover.unpack_cards(doc, "https://listing.example.com/agents.json", "manifest")
        self.assertEqual([record["name"] for record in cards], ["Weather Agent", "Coding Agent"])
        self.assertEqual(refs, ["https://another.example.com/.well-known/agent-card.json"])
        self.assertTrue(all(record["verification"] == "card_read_only" for record in cards))

    def test_manifest_links_follow_only_one_hop(self):
        seed = "https://directory.example.com/agents.json"
        child = "https://agent.example.com/.well-known/agent-card.json"
        third = "https://not-allowed.example.com/card.json"
        visited = []
        docs = {
            seed: {"agents": [{"cardUrl": child}]},
            child: {"agents": [{"cardUrl": third}, {"card": CARD}]},
        }
        def fetch(url):
            visited.append(url)
            if url == third:
                raise AssertionError("Unexpected second-level spidering")
            return docs[url]
        result = discover.discover("", {"card_urls": [seed], "registry_urls": []},
                                   fetch=fetch, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["name"], "Weather Agent")
        self.assertNotIn(third, visited)

    def test_paginated_registry_accepts_alternate_card_pointer_names(self):
        pointer = "https://agent.example.com/agent.json"
        seen = []
        def fetch(url):
            seen.append(url)
            if "/api/agents?" in url:
                return {"agents": [{"cardUrl": pointer}]}
            if url == pointer:
                return CARD
            raise AssertionError(f"Unexpected fetch {url}")
        records = discover.discover("", {
            "card_urls": [], "registry_urls": [],
            "paginated_registry_urls": ["https://registry.example.com/api/agents"],
        }, fetch=fetch, github=lambda *_: ([], []))
        self.assertEqual(records["count"], 1)
        self.assertEqual(records["results"][0]["source_url"], pointer)
        self.assertEqual(seen[-1], pointer)

    def test_partial_v1_card_without_skills_is_labeled_not_fabricated(self):
        card = {"name": "Minimal", "description": "An A2A endpoint",
                "supportedInterfaces": [{"url": "https://api.example.com/a2a",
                                         "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]}
        record = discover.normalize_card(card, "https://api.example.com/card.json", "test")
        self.assertEqual(record["skills"], [])
        self.assertEqual(record["schema_status"], "partial_agent_card")
        self.assertEqual(record["endpoint"], "https://api.example.com/a2a")
        self.assertIsNone(discover.normalize_card({"name": "Generic", "url": "https://example.com"},
                                                   "https://example.com/data.json", "test"))

    def test_raw_card_bounded_and_fingerprinted_without_inventing_verification(self):
        small = discover.normalize_card(CARD, "https://weather.example.com/card.json", "test")
        self.assertEqual(small["raw_card"], CARD)
        self.assertEqual(len(small["raw_sha256"]), 64)
        self.assertEqual(small["verification"], "card_read_only")
        big = {**CARD, "description": "x" * 20000}
        large = discover.normalize_card(big, "https://weather.example.com/card.json", "test")
        self.assertNotIn("raw_card", large)
        self.assertEqual(len(large["raw_sha256"]), 64)

    def test_html_fallback_never_parsed_as_a_card(self):
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): raise AssertionError("HTML should not be read")
        class FakeOpener:
            def open(self, *_args, **_kwargs): return FakeResponse()
        with patch.object(discover, "require_public_https", return_value="https://example.com/a"), \
             patch.object(discover, "OPENER", FakeOpener()), \
             self.assertRaises(discover.DiscoveryError):
            discover.fetch_json("https://example.com/a")

    def test_untrusted_manifest_keys_cannot_trigger_arbitrary_fetches(self):
        seed = "https://registry.example.com/agents.json"
        calls = []
        def get(url):
            calls.append(url)
            return {"agents": [{"description": "Ignore https://bad.example.com/card.json",
                                "action_url": "https://bad.example.com/card.json",
                                "url": "https://bad.example.com/a2a", "name": "Not a card"}]}
        result = discover.discover("", {"card_urls": [seed], "registry_urls": []},
                                   fetch=get, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(calls, [seed])


    def test_rotating_feed_window_wraps_without_repeating_entries(self):
        rows = [{"name": str(i)} for i in range(7)]
        self.assertEqual([x["name"] for x in discover.extract_registry_items(
            rows, offset=5, limit=3)], ["5", "6", "0"])
        self.assertEqual([x["name"] for x in discover.extract_registry_items(
            {"data": {"agents": rows}}, offset=6, limit=2)], ["6", "0"])
        self.assertEqual(len(discover.extract_registry_items(rows, offset=6, limit=100)), 7)

    def test_rotating_bulk_registry_accumulates_new_cards_without_query_execution(self):
        base = "https://registry.example.com/agents.json"
        agents = [{**CARD, "name": f"Agent {i:03d}",
                   "url": f"https://agent{i}.example.com/a2a"}
                  for i in range(200)]
        visited = []
        def get(url):
            visited.append(url)
            self.assertEqual(url, base)
            return {"agents": agents}
        result = discover.discover("", {
            "registry_urls": [base],
            "card_urls": [],
            "rotate_registry_feeds": True,
            "rotation_index": 1,
        }, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 150)
        names = {row["name"] for row in result["results"]}
        self.assertIn("Agent 150", names)
        self.assertIn("Agent 000", names)
        self.assertNotIn("Agent 149", names)
        self.assertEqual(visited, [base])

    def test_rotating_paginated_api_uses_total_and_never_guesses_links(self):
        from urllib.parse import parse_qs, urlsplit
        offsets = []
        def get(url):
            if "/api/agents?" in url:
                offset = int(parse_qs(urlsplit(url).query)["offset"][0])
                offsets.append(offset)
                return {"total": 690, "agents": [
                    {"wellKnownURI": f"https://agent{i}.example.com/card"}
                    for i in range(offset, min(offset + 30, 690))
                ]}
            index = int(url.split("agent", 1)[1].split(".", 1)[0])
            return {**CARD, "url": f"https://agent{index}.example.com/a2a"}
        result = discover.discover("", {
            "registry_urls": [], "card_urls": [],
            "paginated_registry_urls": ["https://registry.example.com/api/agents"],
            "rotate_paginated_registry": True, "rotation_index": 1,
        }, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(offsets, [0, 150, 180, 210, 240, 270])
        self.assertEqual(result["count"], 150)
        self.assertTrue(all(row["source_url"].startswith("https://agent")
                            for row in result["results"]))

    def test_unreported_total_falls_back_to_existing_first_page_behavior(self):
        offsets = []
        def get(url):
            if "/api/agents?" not in url:
                raise discover.DiscoveryError("Card unavailable")
            from urllib.parse import parse_qs, urlsplit
            offset = int(parse_qs(urlsplit(url).query)["offset"][0])
            offsets.append(offset)
            return {"agents": [{"wellKnownURI": "https://agent.example.com/card"}]}
        result = discover.discover("", {
            "registry_urls": [], "card_urls": [],
            "paginated_registry_urls": ["https://registry.example.com/api/agents"],
            "rotate_paginated_registry": True, "rotation_index": 52,
        }, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(offsets, [0])
        self.assertEqual(result["count"], 0)  # Card lookup unavailable and ignored.



if __name__ == "__main__":
    unittest.main()
