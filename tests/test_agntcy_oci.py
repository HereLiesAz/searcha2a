"""Conservative AGNTCY OCI Directory discovery with digest verification."""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover

CARD = {"name": "Flight Agent", "description": "Find routes",
        "url": "https://flight.example.org/a2a",
        "skills": [{"id": "routes", "name": "Find routes", "tags": ["travel"]}]}
ORIGIN = "https://oci.example.org"
REPOSITORY = "published/agents"
ARTIFACT_TYPE = "application/vnd.agntcy.dir.record.v1+json"
LAYER_TYPE = "application/vnd.agntcy.oasf.types.v1alpha2.Record+json"


def fixture(record, inline=True):
    # Hashing constructs a valid test input; asserted outputs are fixed
    # independently (Agent Card names, URLs and data classification).
    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    layer_digest = "sha256:" + hashlib.sha256(body).hexdigest()
    layer = {"mediaType": LAYER_TYPE, "digest": layer_digest}
    if inline:
        layer["data"] = base64.b64encode(body).decode()
    manifest = {"schemaVersion": 2, "artifactType": ARTIFACT_TYPE,
                "layers": [layer]}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    source = {"registry_origin": ORIGIN, "repository": REPOSITORY,
              "manifest_digest": digest, "publish": True}
    manifest_url = ORIGIN + "/v2/" + REPOSITORY + "/manifests/" + digest
    blob_url = ORIGIN + "/v2/" + REPOSITORY + "/blobs/" + layer_digest
    return source, raw, body, manifest_url, blob_url


class AGNTCYOciTests(unittest.TestCase):
    def test_inline_digest_checked_artifact_recognizes_only_a2a_module(self):
        record = {"name": "Generic Record", "modules": [
            {"name": "integration/a2a",
             "artifact": {"media_type": "application/a2a+json", "json": CARD}},
            {"name": "integration/mcp",
             "artifact": {"media_type": "application/a2a+json",
                          "json": {**CARD, "name": "Wrong protocol"}}},
        ]}
        src, raw, body, url, _ = fixture(record)
        visited = []
        def reader(path, auth):
            visited.append((path, auth))
            self.assertEqual(path, url)
            return raw
        cards, links, warnings = discover.agntcy_oci_sources(
            {"agntcy_oci_sources": [src]}, read=reader)
        self.assertEqual([x["name"] for x in cards], ["Flight Agent"])
        self.assertEqual(cards[0]["verification"], "card_read_only")
        self.assertEqual(cards[0]["source"], "agntcy-oci")
        self.assertEqual(cards[0]["source_url"], url)
        self.assertNotIn("raw_card", cards[0])
        self.assertEqual(links, [])
        self.assertEqual(warnings, [])
        self.assertEqual(visited, [(url, "")])

    def test_remote_blob_is_digest_verified_and_linked_cards_are_public(self):
        target = "https://flight.example.org/.well-known/agent-card.json"
        record = {"modules": [
            {"module": {"name": "integration/a2a"},
             "artifact": {"media_type": "application/a2a+json", "url": target}}]}
        src, raw, body, manifest_url, blob_url = fixture(record, inline=False)
        src["token"] = "private-registry-token"
        reads, public_fetches = [], []
        def reader(url, auth):
            reads.append((url, auth))
            return {manifest_url: raw, blob_url: body}[url]
        def get(url):
            public_fetches.append(url)
            if url == target:
                return CARD
            raise discover.DiscoveryError("Proof not found")
        result = discover.discover("", {"agntcy_oci_sources": [src]},
                                   fetch=get, github=lambda *_: ([], []),
                                   oci_read=reader)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["name"], "Flight Agent")
        self.assertEqual(result["results"][0]["source_url"], target)
        self.assertEqual(reads, [(manifest_url, "private-registry-token"),
                                 (blob_url, "private-registry-token")])
        self.assertEqual(public_fetches, [target])
        self.assertNotIn("private-registry-token", json.dumps(result))

    def test_bad_manifest_digest_fails_closed(self):
        record = {"modules": [{"name": "integration/a2a",
                               "artifact": {"media_type": "application/a2a+json",
                                            "json": CARD}}]}
        src, raw, _, _, _ = fixture(record)
        corrupted = raw + b" "
        cards, links, warnings = discover.agntcy_oci_sources(
            {"agntcy_oci_sources": [src]}, read=lambda *_: corrupted)
        self.assertEqual((cards, links), ([], []))
        self.assertEqual(warnings, ["AGNTCY OCI source unavailable"])

    def test_bad_layer_digest_fails_closed(self):
        record = {"modules": [{"name": "integration/a2a",
                               "artifact": {"media_type": "application/a2a+json",
                                            "json": CARD}}]}
        src, raw, body, manifest, blob = fixture(record, inline=False)
        cards, links, warnings = discover.agntcy_oci_sources(
            {"agntcy_oci_sources": [src]},
            read=lambda url, _: raw if url == manifest else body + b"\n")
        self.assertEqual((cards, links), ([], []))
        self.assertEqual(warnings, ["AGNTCY OCI source unavailable"])

    def test_no_implicit_private_publication(self):
        src, raw, _, _, _ = fixture({"modules": []})
        src.pop("publish")
        def forbidden(*_):
            raise AssertionError("No fetch without publish=true")
        self.assertEqual(discover.agntcy_oci_sources(
            {"agntcy_oci_sources": [src]}, read=forbidden), ([], [], []))
        self.assertEqual(discover.agntcy_oci_sources(
            {}, read=forbidden), ([], [], []))

    def test_bad_repo_digests_and_ports_are_not_fetched(self):
        src, raw, _, _, _ = fixture({"modules": []})
        invalid = [
            {**src, "repository": "../../private"},
            {**src, "registry_origin": "https://oci.example.org:8443"},
            {**src, "manifest_digest": "sha256:bad"},
            {**src, "registry_origin": "http://oci.example.org"},
            {**src, "registry_origin": "https://admin:key@oci.example.org"},
        ]
        for source in invalid:
            with self.subTest(source=source):
                cards, links, warnings = discover.agntcy_oci_sources(
                    {"agntcy_oci_sources": [source]},
                    read=lambda *_: (_ for _ in ()).throw(AssertionError()))
                self.assertEqual((cards, links), ([], []))
                self.assertEqual(warnings, ["AGNTCY OCI source unavailable"])

    def test_untyped_oasf_record_does_not_become_an_agent(self):
        record = {"name": "MCP server", "skills": [{"name": "Tool"}],
                  "modules": [{"name": "integration/mcp", "artifact": {
                      "media_type": "application/a2a+json", "json": CARD}}]}
        self.assertEqual(discover.oasf_a2a_cards(record, "https://oci.example.org/x"),
                         ([], []))

    def test_deprecated_inline_card_data_is_bounded_and_not_raw(self):
        record = {"modules": [
            {"name": "integration/a2a", "data": {"a2a_card_data": CARD}}]}
        cards, links = discover.oasf_a2a_cards(
            record, "https://oci.example.org/v2/pub/manifests/sha256:123")
        self.assertEqual([x["name"] for x in cards], ["Flight Agent"])
        self.assertEqual(links, [])
        self.assertNotIn("raw_card", cards[0])

    def test_transport_only_reads_json_https_and_does_not_follow_redirects(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/vnd.oci.image.manifest.v1+json"}
        payload = b'{"schemaVersion":2,"layers":[]}'
        requests = []
        class Opener:
            def open(self, req, timeout):
                requests.append(req)
                return Response(payload)
        with patch.object(discover, "require_public_https",
                          return_value="https://oci.example.org"), \
             patch.object(discover, "OPENER", Opener()):
            actual = discover.fetch_oci_bytes("https://oci.example.org/v2/a/manifests/x",
                                              auth="my-token")
        self.assertEqual(actual, payload)
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer my-token")
        self.assertIsNone(discover.NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://other.example.org/blob"))

    def test_errors_do_not_reflect_private_registry_paths_or_keys(self):
        src, raw, _, _, _ = fixture({"modules": []})
        src["registry_origin"] = "https://private-team.example.org"
        src["token"] = "secret-registry-key"
        def bad(*_):
            raise discover.DiscoveryError("secret-registry-key")
        cards, links, warnings = discover.agntcy_oci_sources(
            {"agntcy_oci_sources": [src]}, read=bad)
        self.assertEqual((cards, links), ([], []))
        self.assertEqual(warnings, ["AGNTCY OCI source unavailable"])
        self.assertNotIn("secret-registry-key", str(warnings))


if __name__ == "__main__":
    unittest.main()
