"""Contract tests for Azphalt's external, non-installable A2A listings."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("azphalt_listings", Path(__file__).parents[1] / "src/azphalt_listings.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ListingsTests(unittest.TestCase):
    def test_maps_card_to_external_directory_record(self):
        catalog = {"generated_at": "2026-11-10T00:00:00Z", "results": [{
            "id": "0123456789abcdefabcd", "name": "Forecast Agent", "description": "Rain",
            "endpoint": "https://forecast.example/api", "source_url": "https://forecast.example/.well-known/agent-card.json",
            "skills": [{"name": "Forecast", "description": "Predict rain", "tags": ["weather"]}],
            "protocols": ["JSONRPC"], "security_schemes": ["oauth2"], "capabilities": {"streaming": True},
        }]}
        result = module.build_listings(catalog)
        self.assertEqual(result["count"], 1)
        item = result["listings"][0]
        self.assertEqual(item["id"], "external.a2a.0123456789abcdefabcd")
        self.assertEqual(item["kind"], "a2a-agent")
        self.assertEqual(item["skills"][0]["name"], "Forecast")
        self.assertEqual(item["security_schemes"], ["oauth2"])
        self.assertEqual(item["discovery_url"], "https://searcha2a.azphalt.store/api/v1/agents/0123456789abcdefabcd.json")
        self.assertIs(item["installable"], False)
        self.assertEqual(item["verification"], "card_read_only")
        self.assertNotIn("license", item)
        self.assertNotIn("llm", item)
        self.assertNotIn("price", item)

    def test_invalid_cards_are_skipped_and_duplicates_removed(self):
        valid = {"id": "00000000000000000001", "name": "A", "source_url": "https://valid.example/agent.json"}
        result = module.build_listings({"results": [valid, valid, {}, {**valid, "id": "bad"}, {**valid, "source_url": "http://internal"}]})
        self.assertEqual(result["count"], 1)

    def test_missing_results_yields_empty_feed(self):
        self.assertEqual(module.build_listings({})["listings"], [])


if __name__ == "__main__":
    unittest.main()
