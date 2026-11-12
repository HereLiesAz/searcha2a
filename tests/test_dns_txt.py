"""Explicit NAIS DNS pointer discovery. No identity claims or agent execution."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover


CARD = {
    "name": "Weather", "description": "forecast",
    "url": "https://agent.example.net/a2a",
    "skills": [{"id": "forecast", "name": "Forecast", "tags": ["weather"]}],
}


class DNSDiscoveryTests(unittest.TestCase):
    def test_explicit_nais_txt_manifest_yields_card_pointer(self):
        requested = []
        def doh(url):
            requested.append(url)
            return {"Status": 0, "AD": False, "Answer": [
                {"name": "_agent.agents.example.org.", "type": 16,
                 "data": '"v=nais1; manifest=https://agent.example.net/card.json; k=ed25519:sample"'}
            ]}
        urls, warnings = discover.dns_txt_sources(
            {"dns_domains": ["agents.example.org"]}, fetch=doh)
        self.assertEqual(requested, [
            "https://dns.google/resolve?name=_agent.agents.example.org&type=TXT"])
        self.assertEqual(urls, [
            ("https://agent.example.net/card.json", "dns-nais")])
        self.assertEqual(warnings, [])

    def test_unset_dns_configuration_makes_no_requests(self):
        def forbidden(_url):
            raise AssertionError("No DNS query should occur")
        self.assertEqual(discover.dns_txt_sources({}, fetch=forbidden), ([], []))
        self.assertEqual(discover.dns_txt_sources(
            {"dns_domains": ["localhost", "192.168.1.1", "corp.internal:53",
                             "https://example.org", "singlelabel", "../../evil"]},
            fetch=forbidden), ([], []))

    def test_nais_fallback_and_multi_part_txt(self):
        def doh(_):
            return {"Status": 0, "Answer": [
                {"name": "_agent.example.org", "type": 16,
                 "data": '"v=nais1; k=ed25519:xyz"'},
                {"name": "_agent.example.org.", "type": 16,
                 "data": '"v=nais1;" " manifest=https://elsewhere.example.net/agent-card.json"'},
            ]}
        urls, warnings = discover.dns_txt_sources(
            {"dns_domains": ["Example.Org."]}, fetch=doh)
        self.assertEqual(urls, [
            ("https://example.org/.well-known/agent.json", "dns-nais"),
            ("https://elsewhere.example.net/agent-card.json", "dns-nais"),
        ])
        self.assertEqual(warnings, [])

    def test_ignore_unrelated_txt_replies_and_unsafe_urls(self):
        def doh(_):
            return {"Status": 0, "Answer": [
                {"name": "_agent.example.org", "type": 16,
                 "data": '"v=aid2; e=https://example.net/a2a"'},
                {"name": "_agent.example.org", "type": 16,
                 "data": '"v=nais1; manifest=http://private.example/card.json"'},
                {"name": "_agent.example.org", "type": 16,
                 "data": '"v=nais1; manifest=https://evil.example:8443/card.json"'},
                {"name": "_agent.another.org", "type": 16,
                 "data": '"v=nais1; manifest=https://bad.example/card.json"'},
                {"name": "_agent.example.org", "type": 5,
                 "data": '"v=nais1; manifest=https://bad.example/card.json"'},
            ]}
        urls, errors = discover.dns_txt_sources(
            {"dns_domains": ["example.org"]}, fetch=doh)
        self.assertEqual(urls, [])
        self.assertEqual(errors, [])

    def test_dns_origin_is_unverified_and_does_not_call_a2a_execution(self):
        card_url = "https://agent.example.net/.well-known/agent.json"
        dns_queries, fetched = [], []
        def doh(url):
            dns_queries.append(url)
            return {"Status": 0, "Answer": [
                {"type": 16, "name": "_agent.example.org",
                 "data": '"v=nais1; manifest=' + card_url + '"'}
            ]}
        def get(url):
            fetched.append(url)
            if url == card_url:
                return CARD
            raise discover.DiscoveryError("Publisher proof not present")
        result = discover.discover("", {"dns_domains": ["example.org"]},
                                   fetch=get, dns_fetch=doh,
                                   github=lambda *_: ([], []))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["source"], "dns-nais")
        self.assertEqual(result["results"][0]["verification"], "card_read_only")
        self.assertNotIn("identity_verified", result["results"][0])
        self.assertEqual(fetched, [card_url])
        self.assertEqual(len(dns_queries), 1)

    def test_doh_error_does_not_leak_response_or_change_registry(self):
        def bad(_):
            raise discover.DiscoveryError("secret-text")
        cards, errors = discover.dns_txt_sources(
            {"dns_domains": ["example.org"]}, fetch=bad)
        self.assertEqual(cards, [])
        self.assertEqual(errors, ["DNS discovery unavailable"])
        self.assertNotIn("secret-text", str(errors))

    def test_wrong_dns_status_and_invalid_answer_shape(self):
        for payload in ({"Status": 2, "Answer": []},
                        {"Status": 0, "Answer": "invalid"},
                        {"Status": 0, "Answer": None},
                        {"Status": 0, "Answer": [{"name": "_agent.example.org",
                                                "type": 16, "data": 123}]}):
            with self.subTest(payload=payload):
                self.assertEqual(discover.dns_txt_sources(
                    {"dns_domains": ["example.org"]}, fetch=lambda _: payload),
                    ([], []))


if __name__ == "__main__":
    unittest.main()
