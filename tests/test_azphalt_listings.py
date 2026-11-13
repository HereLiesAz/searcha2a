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

    def test_ans_claim_is_exported_without_upgrading_verification(self):
        card = {"id": "0123456789abcdefabcd", "name": "Forecast Agent",
                "source_url": "https://forecast.example/card.json",
                "ans_name": "ans://release-2026.11.forecast.example",
                "ans_host": "forecast.example"}
        entry = module.build_listings({"results": [card]})["listings"][0]
        self.assertEqual(entry["ans_claim"], {
            "name": "ans://release-2026.11.forecast.example",
            "agent_host": "forecast.example", "verification": "unverified",
        })
        self.assertIs(entry["installable"], False)
        self.assertEqual(entry["verification"], "card_read_only")

    def test_missing_results_yields_empty_feed(self):
        self.assertEqual(module.build_listings({})["listings"], [])

    # ---- schema_revision 1.1: additive only, Azphalt/Aive pin schema 1 and card_read_only ----

    LEGACY_LISTING_KEYS = {"id", "kind", "name", "description", "endpoint", "source_url",
                           "discovery_url", "skills", "protocols", "security_schemes",
                           "capabilities", "verification", "installable", "source"}
    LEGACY_TOP_KEYS = {"schema", "source", "generated_at", "count", "listings"}

    def _card(self, **extra):
        return {"id": "0123456789abcdefabcd", "name": "Forecast Agent",
                "source_url": "https://forecast.example/card.json",
                "endpoint": "https://forecast.example/a2a", **extra}

    def test_schema_stays_1_and_existing_keys_are_unchanged(self):
        result = module.build_listings({"results": [self._card()]})
        self.assertEqual(result["schema"], 1)
        self.assertEqual(result["schema_revision"], "1.1")
        self.assertTrue(self.LEGACY_TOP_KEYS <= set(result))
        entry = result["listings"][0]
        self.assertTrue(self.LEGACY_LISTING_KEYS <= set(entry))
        self.assertEqual(entry["verification"], "card_read_only")
        self.assertIs(entry["installable"], False)
        self.assertEqual(entry["kind"], "a2a-agent")
        self.assertEqual(entry["source"], "searcha2a")

    def test_legacy_projection_is_byte_identical_for_old_catalogs(self):
        legacy_card = self._card(skills=[{"name": "F", "description": "d", "tags": ["x"]}],
                                 protocols=["JSONRPC"], security_schemes=["oauth2"],
                                 capabilities={"streaming": True})
        entry = module.build_listings({"results": [legacy_card]})["listings"][0]
        added = {"access", "backing", "verification_detail", "registry_checked_at"}
        self.assertEqual({k: v for k, v in entry.items() if k not in added}, {
            "id": "external.a2a.0123456789abcdefabcd", "kind": "a2a-agent", "name": "Forecast Agent",
            "description": "", "endpoint": "https://forecast.example/a2a",
            "source_url": "https://forecast.example/card.json",
            "discovery_url": "https://searcha2a.azphalt.store/api/v1/agents/0123456789abcdefabcd.json",
            "skills": [{"name": "F", "description": "d", "tags": ["x"]}], "protocols": ["JSONRPC"],
            "security_schemes": ["oauth2"], "capabilities": {"streaming": True},
            "verification": "card_read_only", "installable": False, "source": "searcha2a"})

    def test_access_and_backing_come_from_declared_card_data_only(self):
        card = self._card(
            access={"auth_declared": "bearer", "auth_types": ["bearer"], "keyless": "no",
                    "payment_required": "none_declared", "evidence": [{"kind": "x", "path": "y"}], "flags": []},
            backing={"declared_provider": {"organization": "Forecast Co", "url": "https://forecast.example/about",
                                           "url_matches_endpoint": True, "source": "card.provider"},
                     "hosting": {"platform": "vercel", "basis": "endpoint_host_suffix", "host": "x", "shared_platform": True},
                     "model_provider": None, "confidence": "declared", "evidence": [], "unknowns": []})
        entry = module.build_listings({"results": [card]})["listings"][0]
        self.assertEqual(entry["access"], {"auth_types": ["bearer"], "keyless": "no", "payment_required": "none_declared"})
        self.assertEqual(entry["backing"], {"declared_provider": {"organization": "Forecast Co", "url_host": "forecast.example"},
                                            "hosting": "vercel", "model_provider": None})

    def test_invalid_facets_are_dropped_not_trusted(self):
        card = self._card(access={"keyless": "observed", "auth_types": ["x"], "payment_required": "maybe"},
                          backing="nope")
        entry = module.build_listings({"results": [card]})["listings"][0]
        self.assertNotIn("access", entry)
        self.assertNotIn("backing", entry)

    def test_evidence_adds_verification_detail_without_touching_legacy_verification(self):
        evidence = {"0123456789abcdefabcd": {
            "verification_detail": {"level": 2, "level_name": "endpoint_reachable", "reviewed": False,
                                    "checked_at": "2026-11-12T21:10:25Z", "expires_at": "2026-11-19T21:10:25Z",
                                    "computed_at": "2026-11-13T00:00:00Z", "summary": "ignored text"},
            "access": {"keyless": "no", "observed_challenge": "401"}}}
        entry = module.build_listings({"results": [self._card()]}, evidence=evidence)["listings"][0]
        self.assertEqual(entry["verification"], "card_read_only")
        self.assertEqual(entry["verification_detail"], {
            "level": 2, "level_name": "endpoint_reachable", "reviewed": False,
            "checked_at": "2026-11-12T21:10:25Z", "expires_at": "2026-11-19T21:10:25Z"})
        self.assertEqual(entry["registry_checked_at"], "2026-11-13T00:00:00Z")

    def test_missing_or_malformed_evidence_degrades_to_none_never_higher(self):
        for evidence in (None, {}, {"0123456789abcdefabcd": {"verification_detail": {"level": 9,
                "level_name": "hacked", "reviewed": "yes"}}},
                {"0123456789abcdefabcd": "not a dict"}):
            entry = module.build_listings({"results": [self._card()]}, evidence=evidence)["listings"][0]
            self.assertNotIn("verification_detail", entry)
            self.assertEqual(entry["verification"], "card_read_only")

    def test_stale_evidence_is_not_published(self):
        evidence = {"0123456789abcdefabcd": {"verification_detail": {
            "level": 2, "level_name": "endpoint_reachable", "reviewed": False,
            "checked_at": "2026-10-01T00:00:00Z", "expires_at": "2026-10-08T00:00:00Z",
            "computed_at": "2026-11-13T00:00:00Z"}}}
        entry = module.build_listings({"results": [self._card()]}, evidence=evidence,
                                      now="2026-11-13T00:00:00Z")["listings"][0]
        self.assertNotIn("verification_detail", entry)


if __name__ == "__main__":
    unittest.main()
