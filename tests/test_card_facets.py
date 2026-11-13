"""Provider, auth and access facts are derived only from declared card data."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
spec = importlib.util.spec_from_file_location("discover", Path(__file__).parents[1] / "src/discover.py")
discover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discover)

BASE = {
    "name": "Weather", "description": "Rain", "url": "https://weather.example.com/a2a",
    "skills": [{"name": "Forecast", "description": "Predict", "tags": ["weather"]}],
}
SOURCE = "https://weather.example.com/.well-known/agent-card.json"


def norm(**extra):
    return discover.normalize_card({**BASE, **extra}, SOURCE, "test")


class FacetTests(unittest.TestCase):
    def test_provider_is_kept_and_compared_to_endpoint_host(self):
        record = norm(provider={"organization": "Weather Co", "url": "https://weather.example.com"})
        self.assertEqual(record["provider"], {"organization": "Weather Co",
                                              "url": "https://weather.example.com"})
        declared = record["backing"]["declared_provider"]
        self.assertEqual(declared["organization"], "Weather Co")
        self.assertIs(declared["url_matches_endpoint"], True)
        self.assertEqual(record["backing"]["confidence"], "declared")

    def test_github_provider_url_does_not_match_endpoint(self):
        record = norm(provider={"organization": "Someone", "url": "https://github.com/someone"})
        self.assertIs(record["backing"]["declared_provider"]["url_matches_endpoint"], False)

    def test_no_provider_means_unknown_not_guessed(self):
        record = norm(description="Powered by GPT-4o and Claude, running on Anthropic")
        self.assertNotIn("provider", record)
        self.assertIsNone(record["backing"]["declared_provider"])
        self.assertIsNone(record["backing"]["model_provider"])
        self.assertEqual(record["backing"]["confidence"], "unknown")

    def test_hosting_comes_from_endpoint_host_suffix_only(self):
        record = discover.normalize_card({**BASE, "url": "https://x-agent.vercel.app/a2a"},
                                         "https://x-agent.vercel.app/card.json", "test")
        hosting = record["backing"]["hosting"]
        self.assertEqual(hosting["platform"], "vercel")
        self.assertIs(hosting["shared_platform"], True)
        own = norm()["backing"]["hosting"]
        self.assertEqual(own["platform"], "unknown")
        self.assertIs(own["shared_platform"], False)

    def test_structured_model_declaration_only(self):
        ext = {"uri": "https://searcha2a.azphalt.store/ext/backing/v1",
               "params": {"modelProvider": "anthropic", "model": "sonnet"}}
        record = norm(capabilities={"extensions": [ext]})
        self.assertEqual(record["backing"]["model_provider"],
                         {"name": "anthropic", "model": "sonnet", "basis": "card_extension",
                          "confidence": "declared"})

    def test_auth_types_come_from_scheme_type_not_key_name(self):
        record = norm(securitySchemes={
            "licenceKey": {"type": "apiKey", "in": "header", "name": "X-Key"},
            "bearerAuth": {"type": "http", "scheme": "bearer"},
        })
        access = record["access"]
        self.assertEqual(access["auth_types"], ["apiKey", "bearer"])
        self.assertEqual(access["auth_declared"], "multiple")
        self.assertEqual(access["keyless"], "no")
        self.assertEqual(record["security_schemes"], ["bearerAuth", "licenceKey"])

    def test_v1_scheme_shape_is_understood(self):
        record = norm(securitySchemes={"k": {"apiKeySecurityScheme": {"name": "X"}},
                                       "o": {"oauth2SecurityScheme": {}}})
        self.assertEqual(record["access"]["auth_types"], ["apiKey", "oauth"])

    def test_x402_detected_from_extension_scheme_name_and_description(self):
        by_ext = norm(capabilities={"extensions": [
            {"uri": "https://github.com/google-a2a/a2a-x402/v0.1"}]})
        by_name = norm(securitySchemes={"x402": {"type": "apiKey"}})
        by_text = norm(securitySchemes={"p": {"type": "apiKey",
                                              "description": "send PAYMENT-SIGNATURE"}})
        for record in (by_ext, by_name, by_text):
            self.assertIn("x402", record["access"]["auth_types"])
            self.assertEqual(record["access"]["payment_required"], "declared")
            self.assertEqual(record["access"]["keyless"], "no")

    def test_silent_card_is_keyless_declared_never_observed(self):
        access = norm()["access"]
        self.assertEqual(access["auth_declared"], "none_declared")
        self.assertEqual(access["keyless"], "declared")
        self.assertEqual(access["payment_required"], "none_declared")
        self.assertIn(access["keyless"], ("declared", "no", "unknown"))

    def test_prose_claiming_no_key_is_ignored_when_scheme_declared(self):
        record = norm(description="No key needed!", securitySchemes={"b": {"type": "http", "scheme": "bearer"}})
        self.assertEqual(record["access"]["keyless"], "no")

    def test_requirement_without_schemes_is_unknown_not_keyless(self):
        record = norm(security=[{"bearer": []}])
        self.assertEqual(record["access"]["keyless"], "unknown")
        self.assertIn("requirement_without_schemes", record["access"]["flags"])

    def test_facets_survive_when_raw_card_is_dropped_over_4kib(self):
        big = norm(description="x" * 20000, provider={"organization": "Big Co",
                   "url": "https://big.example"}, securitySchemes={"k": {"type": "apiKey"}})
        self.assertNotIn("raw_card", big)
        self.assertEqual(big["provider"]["organization"], "Big Co")
        self.assertEqual(big["access"]["auth_types"], ["apiKey"])
        self.assertEqual(big["access"]["keyless"], "no")

    def test_facets_are_bounded(self):
        record = norm(provider={"organization": "o" * 5000, "url": "https://x.example/" + "p" * 5000},
                      securitySchemes={f"s{i}": {"type": "apiKey"} for i in range(200)})
        self.assertLessEqual(len(json.dumps(record["backing"])) + len(json.dumps(record["access"])), 2048)

    def test_existing_contract_keys_are_unchanged(self):
        record = norm()
        self.assertEqual(record["verification"], "card_read_only")
        for key in ("id", "name", "description", "endpoint", "source_url", "source", "skills",
                    "protocols", "capabilities", "security_schemes", "version", "schema_status",
                    "raw_sha256"):
            self.assertIn(key, record)


if __name__ == "__main__":
    unittest.main()
