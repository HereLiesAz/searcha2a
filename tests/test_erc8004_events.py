"""ERC-8004 IdentityRegistry read-only event decoding, not owner verification."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover


CONTRACT = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
REGISTERED = "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a"
UPDATED = "0x3a2c7fffc2cba7582c690e3b82c453ea02a308326a98a3ad7576c606336409fb"
REGISTRATION = "https://agent.example.net/registration.json"
# ABI-encoded dynamic string fixture, independently constructed using the
# Solidity ABI specification for a 43-byte URL, including right padding.
DATA = (
    "0x0000000000000000000000000000000000000000000000000000000000000020"
    "000000000000000000000000000000000000000000000000000000000000002b"
    "68747470733a2f2f6167656e742e6578616d706c652e6e65742f726567697374"
    "726174696f6e2e6a736f6e000000000000000000000000000000000000000000"
)
EVENT = {
    "address": CONTRACT,
    "topics": [REGISTERED, "0x" + "0" * 63 + "2", "0x" + "0" * 24 + "f" * 40],
    "data": DATA,
    "removed": False,
}
SOURCE = {"rpc_url": "https://rpc.example.org/read-only", "registry_address": CONTRACT,
          "enabled": True, "from_block": 100, "to_block": 111}


class ERC8004EventTests(unittest.TestCase):
    def test_event_topics_match_published_identity_registry_abi(self):
        self.assertEqual(discover.REGISTERED_TOPIC, REGISTERED)
        self.assertEqual(discover.URI_UPDATED_TOPIC, UPDATED)

    def test_registered_and_updated_events_decode_the_exact_registration_uri(self):
        self.assertEqual(discover.erc8004_event_uri(EVENT, CONTRACT), REGISTRATION)
        self.assertEqual(discover.erc8004_event_uri(
            {**EVENT, "topics": [UPDATED, *EVENT["topics"][1:]]}, CONTRACT),
            REGISTRATION)

    def test_invalid_or_mismatched_events_cannot_add_urls(self):
        invalid = [
            {**EVENT, "address": "0x" + "1" * 40},
            {**EVENT, "topics": ["0x" + "2" * 64, *EVENT["topics"][1:]]},
            {**EVENT, "topics": [REGISTERED]},
            {**EVENT, "removed": True},
            {**EVENT, "data": DATA[:100]},
            {**EVENT, "data": "0x" + "0" * 128},
            {**EVENT, "data": "0xnothex"},
        ]
        for log in invalid:
            with self.subTest(log=log):
                self.assertEqual(discover.erc8004_event_uri(log, CONTRACT), "")

    def test_fixed_window_requests_only_whitelisted_read_methods(self):
        called = []
        def rpc(url, method, params):
            called.append((url, method, params))
            return {"result": [EVENT]}
        urls, warnings = discover.erc8004_event_sources(
            {"erc8004_rpc_sources": [SOURCE]}, post=rpc)
        self.assertEqual(urls, [(REGISTRATION, "erc8004-registration")])
        self.assertEqual(warnings, [])
        self.assertEqual(called, [(
            "https://rpc.example.org/read-only", "eth_getLogs",
            [{"address": CONTRACT, "fromBlock": "0x64", "toBlock": "0x6f",
              "topics": [[REGISTERED, UPDATED]]}]
        )])

    def test_uri_updated_supersedes_registered_within_a_window(self):
        updated_data = (
            "0x0000000000000000000000000000000000000000000000000000000000000020"
            "0000000000000000000000000000000000000000000000000000000000000023"
            "68747470733a2f2f6167656e742e6578616d706c652e6e65742f6e657874"
            "2e6a736f6e0000000000000000000000000000000000000000000000000000000000"
        )
        next_event = {**EVENT, "topics": [UPDATED, *EVENT["topics"][1:]],
                      "data": updated_data}
        def rpc(url, method, params):
            return {"result": [EVENT, next_event]}
        urls, warnings = discover.erc8004_event_sources(
            {"erc8004_rpc_sources": [SOURCE]}, post=rpc)
        self.assertEqual(urls, [
            ("https://agent.example.net/next.json", "erc8004-registration")])
        self.assertEqual(warnings, [])

    def test_latest_block_window_is_bounded(self):
        calls = []
        def rpc(url, method, params):
            calls.append((method, params))
            if method == "eth_blockNumber":
                return {"result": "0x3e8"}
            return {"result": [EVENT]}
        cards, _ = discover.erc8004_event_sources(
            {"erc8004_rpc_sources": [{
                "rpc_url": "https://rpc.example.org", "registry_address": CONTRACT,
                "enabled": True, "lookback_blocks": 128,
            }]}, post=rpc)
        self.assertEqual(cards, [(REGISTRATION, "erc8004-registration")])
        self.assertEqual([x[0] for x in calls], ["eth_blockNumber", "eth_getLogs"])
        self.assertEqual(calls[1][1][0]["fromBlock"], "0x369")
        self.assertEqual(calls[1][1][0]["toBlock"], "0x3e8")

    def test_invalid_bounds_fail_closed_without_rpc(self):
        def forbidden(*_):
            raise AssertionError("Misconfigured source must not call the RPC")
        for candidate in [
            {**SOURCE, "from_block": 20, "to_block": 532},
            {**SOURCE, "from_block": -1, "to_block": 1},
            {**SOURCE, "from_block": True, "to_block": 5},
            {**SOURCE, "to_block": None},
            {**SOURCE, "registry_address": "0x1234"},
            {**SOURCE, "enabled": False},
        ]:
            with self.subTest(candidate=candidate):
                cards, warnings = discover.erc8004_event_sources(
                    {"erc8004_rpc_sources": [candidate]}, post=forbidden)
                self.assertEqual(cards, [])
                self.assertTrue(warnings or candidate["enabled"] is False)

    def test_disabled_by_default(self):
        self.assertEqual(discover.erc8004_event_sources(
            {}, post=lambda *_: (_ for _ in ()).throw(AssertionError())), ([], []))

    def test_truncated_log_window_warns_and_caps_requests(self):
        def rpc(url, method, params):
            return {"result": [EVENT] * 121}
        urls, warnings = discover.erc8004_event_sources(
            {"erc8004_rpc_sources": [SOURCE]}, post=rpc)
        self.assertEqual(urls, [(REGISTRATION, "erc8004-registration")])
        self.assertEqual(warnings, ["ERC-8004 log window truncated"])

    def test_registration_is_only_a_card_pointer_not_an_identity_claim(self):
        card_url = "https://agent.example.net/.well-known/agent-card.json"
        visited = []
        documents = {
            REGISTRATION: {
                "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
                "name": "Chain ID 2", "services": [
                    {"name": "A2A", "endpoint": card_url},
                    {"name": "MCP", "endpoint": "https://agent.example.net/mcp"}],
            },
            card_url: {
                "name": "Event Agent", "description": "Metadata only",
                "url": "https://agent.example.net/a2a",
                "skills": [{"id": "f", "name": "Forecast", "tags": ["weather"]}],
            },
        }
        def get(url):
            visited.append(url)
            if url in documents:
                return documents[url]
            raise discover.DiscoveryError("No proof")
        result = discover.discover("", {"erc8004_rpc_sources": [SOURCE]},
            fetch=get, github=lambda *_: ([], []),
            chain_rpc=lambda *_: {"result": [EVENT]})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["name"], "Event Agent")
        self.assertEqual(result["results"][0]["verification"], "card_read_only")
        self.assertNotIn("identity_verified", result["results"][0])
        self.assertEqual(visited, [REGISTRATION, card_url])

    def test_rpc_transport_rejects_write_methods_and_bounded_json(self):
        for method in ("eth_sendTransaction", "eth_sendRawTransaction", "personal_sign"):
            with self.assertRaises(discover.DiscoveryError):
                discover.post_chain_rpc("https://rpc.example.org", method, [])
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        requests = []
        class Opener:
            def open(self, req, timeout):
                requests.append(req)
                return Response(json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "result": []}).encode("utf-8"))
        with patch.object(discover, "require_public_https",
                          return_value="https://rpc.example.org"), \
             patch.object(discover, "OPENER", Opener()):
            reply = discover.post_chain_rpc(
                "https://rpc.example.org", "eth_getLogs", [{"address": CONTRACT}])
        self.assertEqual(reply["result"], [])
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertEqual(json.loads(requests[0].data)["method"], "eth_getLogs")

    def test_invalid_rpc_source_cannot_leak_url_keys_in_public_warning(self):
        source = {**SOURCE, "rpc_url": "https://rpc.example.org/secret-API-key"}
        def failure(*_):
            raise discover.DiscoveryError("secret-API-key")
        cards, warnings = discover.erc8004_event_sources(
            {"erc8004_rpc_sources": [source]}, post=failure)
        self.assertEqual(cards, [])
        self.assertEqual(warnings, ["ERC-8004 source unavailable"])
        self.assertNotIn("secret-API-key", str(warnings))


if __name__ == "__main__":
    unittest.main()
