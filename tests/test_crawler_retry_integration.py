"""Scheduler integration: genuine public retries without private catalog leakage."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover
import crawl_retry

CARD_URL = "https://agent.example.org/.well-known/agent-card.json"
MANIFEST_URL = "https://directory.example.org/agents.json"
PRIVATE_URL = "https://enterprise.example.org/private/card.json"
CARD = {"name": "Public Weather", "description": "Forecast",
        "url": "https://agent.example.org/a2a",
        "skills": [{"id": "forecast", "name": "Forecast", "tags": ["weather"]}]}


class CrawlerRetryIntegrationTests(unittest.TestCase):
    def test_discovered_rotating_public_card_retried_without_seed_reappearance(self):
        state = {"version": 1, "run_day": 0, "pending": []}
        health = {}
        def failed(url):
            self.assertEqual(url, CARD_URL)
            raise discover.DiscoveryError("Temporary service outage")
        result = discover.discover("", {
            "paginated_registry_urls": [], "card_urls": [],
            "registry_urls": [MANIFEST_URL], "rotation_index": 1,
        }, fetch=lambda url: (
            {"agents": [{"cardUrl": CARD_URL}]} if url == MANIFEST_URL
            else failed(url)), retry_state=state, health_report=health,
            retry_day=1000, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(state["pending"], [{
            "url": CARD_URL, "source": "registry", "failures": 1,
            "retry_day": 1001,
        }])
        self.assertEqual(health["unavailable"], 1)
        self.assertEqual(health["pending"], 1)
        self.assertEqual(health["by_source"]["registry"]["attempted"], 1)
        self.assertNotIn("agent.example.org", json.dumps(health))
        fetched = []
        def good(url):
            fetched.append(url)
            if url == CARD_URL:
                return CARD
            raise discover.DiscoveryError("Unrecognized URL")
        # Registry no longer provides the URL. A saved public retry remains.
        result = discover.discover("", {}, fetch=good,
             retry_state=state, health_report={}, retry_day=1001,
             github=lambda *_: ([], []))
        self.assertEqual(fetched, [CARD_URL])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["source"], "registry")
        self.assertEqual(result["results"][0]["source_url"], CARD_URL)
        self.assertEqual(state["pending"], [])

    def test_deferred_url_skipped_even_if_new_public_registry_repeats_it(self):
        state = {"version": 1, "run_day": 1000, "pending": [{
            "url": CARD_URL, "source": "registry", "failures": 2,
            "retry_day": 1003,
        }]}
        fetched = []
        def get(url):
            fetched.append(url)
            if url == MANIFEST_URL:
                return {"agents": [{"cardUrl": CARD_URL}]}
            raise AssertionError("Do not request deferred card")
        report = {}
        result = discover.discover("", {"registry_urls": [MANIFEST_URL]},
            fetch=get, retry_state=state, health_report=report, retry_day=1001,
            github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(fetched, [MANIFEST_URL])
        self.assertEqual(report["retry_deferred"], 1)
        self.assertEqual(state["pending"][0]["retry_day"], 1003)

    def test_private_catalog_card_never_enters_public_retry_or_health(self):
        state = {"version": 1, "run_day": 0, "pending": []}
        requests = []
        def private_get(url, scheme, token):
            requests.append((url, scheme, token))
            return {"agents": [{"cardUrl": PRIVATE_URL}]}
        def failed(url):
            self.assertEqual(url, PRIVATE_URL)
            raise discover.DiscoveryError("Sensitive endpoint not available")
        health = {}
        result = discover.discover("", {"private_catalogs": [{
            "url": "https://enterprise.example.org/internal/feed.json",
            "auth_type": "bearer", "token": "private-token",
            "publish": True,
        }]}, fetch=failed, private_fetch=private_get,
            retry_state=state, health_report=health, retry_day=1000,
            github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(state["pending"], [])
        self.assertEqual(health["attempted"], 0)
        serialized = json.dumps({"state": state, "health": health})
        self.assertNotIn("enterprise.example.org", serialized)
        self.assertNotIn("private-token", serialized)

    def test_private_manifest_followups_never_become_public_retries(self):
        state = {"version": 1, "run_day": 0, "pending": []}
        root = "https://enterprise.example.org/private/manifest.json"
        child = "https://enterprise.example.org/private/child.json"
        def private_get(url, *_):
            self.assertEqual(url, "https://enterprise.example.org/private-feed.json")
            return {"agents": [{"cardUrl": root}]}
        hits = []
        def fetch(url):
            hits.append(url)
            if url == root:
                return {"agents": [{"cardUrl": child}]}
            if url == child:
                raise discover.DiscoveryError("Unavailable internal child")
            raise AssertionError(url)
        health = {}
        discover.discover("", {"private_catalogs": [{
            "url": "https://enterprise.example.org/private-feed.json",
            "auth_type": "api_key", "token": "very-private", "publish": True,
        }]}, fetch=fetch, private_fetch=private_get,
            retry_state=state, health_report=health, retry_day=1000,
            github=lambda *_: ([], []))
        self.assertEqual(hits, [root, child])
        self.assertEqual(state["pending"], [])
        self.assertEqual(health["attempted"], 0)

    def test_noncard_html_cannot_become_a_persisted_retry(self):
        state = {"version": 1, "run_day": 0, "pending": []}
        report = {}
        def invalid(url):
            raise discover.DiscoveryError("Non-JSON response Content-Type")
        result = discover.discover("", {"card_urls": [CARD_URL]},
             fetch=invalid, retry_state=state, health_report=report,
             retry_day=1000, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(state["pending"], [])
        self.assertEqual(report["invalid"], 1)
        self.assertEqual(report["unavailable"], 0)

    def test_retries_do_not_run_from_interactive_query(self):
        state = {"version": 1, "run_day": 1000, "pending": [{
            "url": CARD_URL, "source": "registry", "failures": 1, "retry_day": 1001}]}
        result = discover.discover("forecast", {}, fetch=lambda *_: (
            _ for _ in ()).throw(AssertionError("Interactive query must not retry")),
            retry_state=state, retry_day=1001, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 0)
        self.assertEqual(state["pending"][0]["retry_day"], 1001)


if __name__ == "__main__":
    unittest.main()
