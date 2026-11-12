"""Opt-in MCP 2026-07-28 resources discovery with no tools/call."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover


ENDPOINT = "https://directory.example.org/mcp"
CARD = {
    "name": "History Agent", "description": "Lookup historical events",
    "url": "https://agents.example.org/history/a2a",
    "skills": [{"id": "history", "name": "History", "tags": ["research"]}],
}
MIME = "application/a2a+json"


class MCPResourceDiscoveryTests(unittest.TestCase):
    def test_default_configuration_never_posts_to_servers(self):
        def forbidden(*_):
            raise AssertionError("MCP should not be contacted")
        self.assertEqual(discover.mcp_metadata_sources(
            {}, post=forbidden), ([], [], []))
        self.assertEqual(discover.mcp_metadata_sources({
            "mcp_sources": [{"endpoint": ENDPOINT}]}, post=forbidden),
            ([], [], []))

    def test_explicit_listing_fetches_only_typed_public_card_gets(self):
        target = "https://agents.example.org/history/card.json"
        observed = []
        def rpc(url, method, params, token):
            observed.append((url, method, params, token))
            return {"resources": [
                {"uri": target, "mimeType": MIME},
                {"uri": "https://evil.example.org/not-card.json", "mimeType": "text/plain"},
                {"uri": "file:///etc/passwd", "mimeType": MIME},
            ]}
        fetched = []
        def get(url):
            fetched.append(url)
            if url == target:
                return CARD
            raise discover.DiscoveryError("No proof")
        result = discover.discover("", {"mcp_sources": [
            {"endpoint": ENDPOINT, "publish": True}]},
            mcp_post=rpc, fetch=get, github=lambda *_: ([], []))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["source_url"], target)
        self.assertEqual(result["results"][0]["verification"], "card_read_only")
        self.assertEqual(fetched, [target])
        self.assertEqual(observed, [(ENDPOINT, "resources/list", {}, "")])

    def test_opaque_a2a_resource_requires_separate_read_permission(self):
        calls = []
        def rpc(url, method, params, token):
            calls.append((method, params, token))
            if method == "resources/list":
                return {"resources": [{"uri": "agent-card://directory.example.org/history",
                                       "mimeType": MIME}]}
            if method == "resources/read":
                return {"contents": [{
                    "mimeType": MIME, "uri": "agent-card://directory.example.org/history",
                    "text": json.dumps(CARD),
                }]}
            raise AssertionError("No other RPC methods permitted")
        entries = [{"endpoint": ENDPOINT, "publish": True, "token": "private-secret",
                    "allow_resource_read": True}]
        cards, urls, warnings = discover.mcp_metadata_sources(
            {"mcp_sources": entries}, post=rpc)
        self.assertEqual([c["name"] for c in cards], ["History Agent"])
        self.assertEqual(cards[0]["source"], "mcp-resource")
        self.assertEqual(urls, [])
        self.assertNotIn("raw_card", cards[0])
        self.assertEqual(warnings, [])
        self.assertEqual([c[0] for c in calls], ["resources/list", "resources/read"])
        self.assertEqual(calls[1][1], {"uri": "agent-card://directory.example.org/history"})
        self.assertNotIn("private-secret", json.dumps(cards))
        entries[0]["allow_resource_read"] = False
        calls.clear()
        cards, urls, errors = discover.mcp_metadata_sources(
            {"mcp_sources": entries}, post=rpc)
        self.assertEqual(cards, [])
        self.assertEqual([c[0] for c in calls], ["resources/list"])

    def test_file_and_local_resources_cannot_be_read(self):
        self.assertFalse(discover.mcp_resource_card_uri("file:///etc/passwd"))
        self.assertFalse(discover.mcp_resource_card_uri("http://localhost/private"))
        self.assertFalse(discover.mcp_resource_card_uri("https://local.example/card.json"))
        self.assertFalse(discover.mcp_resource_card_uri("a2a://user:pass@example.org/x"))
        self.assertFalse(discover.mcp_resource_card_uri("a2a://example.org/x#fragment"))
        self.assertTrue(discover.mcp_resource_card_uri("a2a://public.example.org/card"))

    def test_pagination_stops_at_three_pages_and_deduplicates_uris(self):
        cursors = []
        def rpc(url, method, params, token):
            self.assertEqual(method, "resources/list")
            cursors.append(params.get("cursor"))
            return {"resources": [{"uri": "https://agent.example.org/card.json",
                                   "mimeType": MIME}],
                    "nextCursor": "page" + str(len(cursors))}
        cards, links, warnings = discover.mcp_metadata_sources(
            {"mcp_sources": [{"endpoint": ENDPOINT, "publish": True}]},
            post=rpc)
        self.assertEqual(cursors, [None, "page1", "page2"])
        self.assertEqual(cards, [])
        self.assertEqual(links, ["https://agent.example.org/card.json"])
        self.assertEqual(warnings, [])

    def test_read_budget_caps_typed_resources_at_ten(self):
        count = []
        def rpc(url, method, params, token):
            count.append(method)
            if method == "resources/list":
                return {"resources": [{"uri": f"agent://directory.example.org/{i}",
                                       "mimeType": MIME} for i in range(40)]}
            return {"contents": []}
        result = discover.mcp_metadata_sources(
            {"mcp_sources": [{"endpoint": ENDPOINT, "publish": True,
                              "allow_resource_read": True}]}, post=rpc)
        self.assertEqual(result, ([], [], []))
        self.assertEqual(count.count("resources/list"), 1)
        self.assertEqual(count.count("resources/read"), 10)

    def test_no_tasks_or_tools_or_unbounded_params_allowed(self):
        for method in ("tools/call", "message/send", "tasks/get", "initialize",
                       "resources/subscribe", "resources/delete"):
            with self.assertRaises(discover.DiscoveryError):
                discover.post_mcp_metadata(ENDPOINT, method, {})
        with self.assertRaises(discover.DiscoveryError):
            discover.post_mcp_metadata(ENDPOINT, "resources/read",
                                       {"uri": "a2a://evil", "arguments": {}})

    def test_opaque_resources_without_http_endpoint_keep_distinct_ids(self):
        names = ["First Public Directory Agent", "Second Public Directory Agent"]
        uri1 = "agent-card://directory.example.org/first"
        uri2 = "agent-card://directory.example.org/second"
        def rpc(_url, method, params, _token):
            if method == "resources/list":
                return {"resources": [
                    {"uri": uri1, "mimeType": MIME},
                    {"uri": uri2, "mimeType": MIME},
                ]}
            uri = params["uri"]
            name = names[0] if uri == uri1 else names[1]
            return {"contents": [{"mimeType": MIME, "text": json.dumps({
                "name": name, "description": "No public HTTP endpoint",
                "skills": [{"name": "Lookup", "tags": ["search"]}],
            })}]}
        response = discover.discover("", {"mcp_sources": [{
            "endpoint": ENDPOINT, "publish": True, "allow_resource_read": True,
        }]}, mcp_post=rpc, github=lambda *_: ([], []))
        self.assertEqual(response["count"], 2)
        self.assertEqual([x["name"] for x in response["results"]], names)
        self.assertEqual(len({x["id"] for x in response["results"]}), 2)
        self.assertEqual({x["source_url"] for x in response["results"]}, {ENDPOINT})

    def test_unavailable_resource_does_not_discard_siblings_or_other_links(self):
        url = "https://agent.example.org/metadata/card.json"
        broken = "agent-card://directory.example.org/missing"
        healthy = "agent-card://directory.example.org/available"
        calls = []
        def rpc(_url, method, params, _token):
            calls.append((method, dict(params)))
            if method == "resources/list":
                return {"resources": [
                    {"uri": url, "mimeType": MIME},
                    {"uri": broken, "mimeType": MIME},
                    {"uri": healthy, "mimeType": MIME},
                ]}
            if params["uri"] == broken:
                raise discover.DiscoveryError("MCP resource deleted")
            return {"contents": [{"mimeType": MIME, "text": json.dumps(CARD)}]}
        cards, links, warnings = discover.mcp_metadata_sources({
            "mcp_sources": [{
                "endpoint": ENDPOINT, "publish": True, "allow_resource_read": True,
            }]}, post=rpc)
        self.assertEqual([x["name"] for x in cards], ["History Agent"])
        self.assertEqual(links, [url])
        self.assertEqual(warnings, [])
        self.assertEqual([x[0] for x in calls],
                         ["resources/list", "resources/read", "resources/read"])

    def test_deeply_nested_rpc_response_is_reported_not_raised(self):
        deeply_nested = "[" * 20000 + "0" + "]" * 20000
        response = '{"jsonrpc":"2.0","id":1,"result":{"resources":' + deeply_nested + '}}'
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        class Opener:
            def open(self, request, timeout):
                return Response(response.encode("utf-8"))
        with patch.object(discover, "require_public_https", return_value=ENDPOINT), \
             patch.object(discover, "OPENER", Opener()), \
             self.assertRaises(discover.DiscoveryError):
            discover.post_mcp_metadata(ENDPOINT, "resources/list", {})
        # A mocked connector can also fail similarly; other sources survive.
        def fail(_url, _method, _params, _token):
            raise RecursionError("malformed MCP response")
        self.assertEqual(discover.mcp_metadata_sources({
            "mcp_sources": [{"endpoint": ENDPOINT, "publish": True}]}, post=fail),
            ([], [], ["MCP discovery unavailable"]))

    def test_deeply_nested_resource_text_does_not_abort_other_cards(self):
        bad = "agent-card://directory.example.org/deep"
        good = "agent-card://directory.example.org/healthy"
        nested = "[" * 20000 + "0" + "]" * 20000
        def rpc(_url, method, params, _token):
            if method == "resources/list":
                return {"resources": [
                    {"uri": bad, "mimeType": MIME},
                    {"uri": good, "mimeType": MIME},
                ]}
            return {"contents": [{"mimeType": MIME, "text":
                                  nested if params["uri"] == bad else json.dumps(CARD)}]}
        cards, links, warnings = discover.mcp_metadata_sources({
            "mcp_sources": [{
                "endpoint": ENDPOINT, "publish": True, "allow_resource_read": True,
            }]}, post=rpc)
        self.assertEqual([c["name"] for c in cards], ["History Agent"])
        self.assertEqual(links, [])
        self.assertEqual(warnings, [])

    def test_post_transport_uses_stateless_mcp_headers_and_body(self):
        requests = []
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                method = json.loads(request.data)["method"]
                return Response(json.dumps({
                    "jsonrpc": "2.0", "id": 1 if method == "resources/list" else 2,
                    "result": {"resources": []} if method == "resources/list" else
                              {"contents": []},
                }).encode())
        with patch.object(discover, "require_public_https", return_value=ENDPOINT), \
             patch.object(discover, "OPENER", Opener()):
            discover.post_mcp_metadata(ENDPOINT, "resources/list", {})
            discover.post_mcp_metadata(ENDPOINT, "resources/read",
                                       {"uri": "a2a://directory.example.org/history"},
                                       "private-token")
        self.assertEqual([r.get_method() for r in requests], ["POST", "POST"])
        self.assertEqual(requests[0].get_header("Mcp-method"), "resources/list")
        self.assertEqual(requests[0].get_header("Mcp-protocol-version"), "2026-07-28")
        body = json.loads(requests[0].data)
        self.assertEqual(body["params"]["_meta"][
            "io.modelcontextprotocol/protocolVersion"], "2026-07-28")
        self.assertEqual(requests[1].get_header("Mcp-name"),
                         "a2a://directory.example.org/history")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer private-token")

    def test_streaming_responses_fail_closed(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/event-stream"}
        class Opener:
            def open(self, *_args, **_kwargs):
                return Response(b"event: message\ndata: {\"jsonrpc\":\"2.0\"}\n\n")
        with patch.object(discover, "require_public_https", return_value=ENDPOINT), \
             patch.object(discover, "OPENER", Opener()), \
             self.assertRaisesRegex(discover.DiscoveryError, "bounded JSON"):
            discover.post_mcp_metadata(ENDPOINT, "resources/list", {})

    def test_private_errors_do_not_expose_credentials(self):
        def failure(*_):
            raise discover.DiscoveryError("private-secret")
        cards, links, warnings = discover.mcp_metadata_sources(
            {"mcp_sources": [{
                "endpoint": ENDPOINT, "publish": True, "token": "private-secret",
            }]}, post=failure)
        self.assertEqual((cards, links), ([], []))
        self.assertEqual(warnings, ["MCP discovery unavailable"])
        self.assertNotIn("private-secret", json.dumps(warnings))


if __name__ == "__main__":
    unittest.main()
