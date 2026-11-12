"""Agent Card ANS claims are metadata, not authenticated publisher identities."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from discover import normalize_card


class ANSDiscoveryTests(unittest.TestCase):
    def test_publisher_defined_version_survives_normalization(self):
        card = {"name": "Weather", "url": "https://weather.example.com/a2a",
                "skills": [{"name": "forecast"}], "agentHost": "weather.example.com",
                "ansName": "ans://release-2026.11.weather.example.com"}
        result = normalize_card(card, "https://weather.example.com/.well-known/agent-card.json", "test")
        self.assertEqual(result["ans_name"], "ans://release-2026.11.weather.example.com")
        self.assertEqual(result["ans_host"], "weather.example.com")
        self.assertEqual(result["verification"], "card_read_only")

    def test_claim_without_host_is_not_inferred_from_endpoint(self):
        card = {"name": "Weather", "url": "https://weather.example.com/a2a",
                "skills": [{"name": "forecast"}], "ansName": "ans://3.weather.example.com"}
        result = normalize_card(card, "https://weather.example.com/card.json", "test")
        self.assertNotIn("ans_name", result)


if __name__ == "__main__":
    unittest.main()
