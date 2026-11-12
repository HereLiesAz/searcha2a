"""Publisher verification and safe generation of reviewed, signed-llm-ready sources."""
import importlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from azphalt_promote import PREFIX, _id, promote
from verify_agent import PROOF_PATH, verify_publisher

CARD_URL = "https://weather.example.org/.well-known/agent-card.json"
API_URL = "https://weather.example.org/a2a"
PROOF_URL = "https://weather.example.org" + PROOF_PATH

CARD = {
    "name": "Weather Bot",
    "description": "Accurate weather agent",
    "url": API_URL,
    "skills": [{"name": "Forecast", "description": "Weather"}],
    "supportedInterfaces": [{"protocolBinding": "JSONRPC", "url": API_URL}],
    "azphalt": {"externalAgentListing": True},
}

def proof_at(issued):
    return {
        "schema": 1, "listing": "azphalt-external-a2a", "consent": True, "license": "MIT",
        "card_url": CARD_URL, "endpoint": API_URL, "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(days=3)).isoformat(),
    }

def approved_feed():
    return {
        "schema": 1, "source": "searcha2a", "listings": [{
            "kind": "a2a-agent", "installable": False, "verification": "card_read_only",
            "source_url": CARD_URL, "publisher_verification": {"method": "https-origin-opt-in"},
        }],
    }

class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.proof = proof_at(self.now - timedelta(hours=1))
        self.requests = []

    def fetch(self, url):
        self.requests.append(url)
        return {CARD_URL: CARD, PROOF_URL: self.proof}[url]

    def test_verified_opt_in_produces_declarative_llm_manifest_and_generated_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = promote(approved_feed(), root, fetch=self.fetch)
            self.assertEqual(report, {"created": [_id(CARD_URL)], "updated": [], "revoked": []})
            folder = root / _id(CARD_URL)
            manifest = json.loads((folder / "manifest.json").read_text())
            self.assertEqual(manifest["kind"], "llm")
            self.assertEqual(manifest["license"], "MIT")
            self.assertEqual(manifest["llm"]["endpoint"]["protocols"], ["a2a-jsonrpc"])
            self.assertEqual(manifest["llm"]["endpoint"]["baseUrl"], API_URL)
            self.assertEqual(manifest["llm"]["setup"]["script"], "setup/setup.sh")
            self.assertEqual(manifest["llm"]["dataHandling"]["prompts"], "unknown")
            self.assertEqual(json.loads((folder / "setup/agent.json").read_text())["agentCardUrl"], CARD_URL)
            self.assertIn("MIT License", (folder / "LICENSE").read_text())
            process = subprocess.run(["bash", str(folder / "setup/setup.sh")], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("Host must revalidate", process.stdout)
            # One re-fetch of original card, then one same-origin proof. Never the execution endpoint.
            self.assertEqual(self.requests, [CARD_URL, PROOF_URL])

    def test_no_opt_in_evidence_never_promotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = approved_feed()
            del feed["listings"][0]["publisher_verification"]
            report = promote(feed, Path(tmp), fetch=self.fetch)
            self.assertEqual(report["created"], [])
            self.assertEqual(self.requests, [])

    def test_cannot_promote_stale_or_changed_origin_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.proof = proof_at(self.now - timedelta(days=10))
            self.assertEqual(promote(approved_feed(), Path(tmp), fetch=self.fetch)["created"], [])
            self.proof = proof_at(self.now - timedelta(hours=1))
            self.proof["card_url"] = "https://other.example.org/card"
            self.assertEqual(promote(approved_feed(), Path(tmp), fetch=self.fetch)["created"], [])

    def test_generator_never_executes_card_content_as_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "unexpected"
            evil_card = {**CARD, "name": '$(touch ' + str(marker) + ')'}
            def fetch(url):
                return {CARD_URL: evil_card, PROOF_URL: self.proof}[url]
            report = promote(approved_feed(), Path(tmp), fetch=fetch)
            self.assertEqual(len(report["created"]), 1)
            folder = Path(tmp) / report["created"][0]
            subprocess.run(["bash", str(folder / "setup/setup.sh")], check=True, capture_output=True)
            self.assertFalse(marker.exists())

    def test_same_source_is_idempotent_and_updates_bump_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = promote(approved_feed(), root, fetch=self.fetch)
            self.assertEqual(len(first["created"]), 1)
            second = promote(approved_feed(), root, fetch=self.fetch)
            self.assertEqual(second["updated"], [])
            self.assertEqual(second["created"], [])
            changed = {**CARD, "description": "Changed weather description"}
            def fetch(url):
                return {CARD_URL: changed, PROOF_URL: self.proof}[url]
            third = promote(approved_feed(), root, fetch=fetch)
            self.assertEqual(third["updated"], first["created"])
            manifest = json.loads((root / first["created"][0] / "manifest.json").read_text())
            self.assertEqual(manifest["version"], "1.0.1")

    def test_unavailable_proof_never_deletes_prior_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = promote(approved_feed(), root, fetch=self.fetch)
            def failing(url):
                raise OSError("origin is temporarily down")
            two = promote(approved_feed(), root, fetch=failing)
            self.assertEqual(two["revoked"], one["created"])
            self.assertTrue((root / one["created"][0] / "manifest.json").is_file())
            self.assertEqual(two["created"], [])

    def test_refuses_unrecognized_existing_id_and_out_of_bounds_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / _id(CARD_URL)
            folder.mkdir()
            (folder / "manifest.json").write_text(json.dumps({"id": folder.name, "author": "somebody else"}))
            with self.assertRaisesRegex(ValueError, "Refusing"):
                promote(approved_feed(), root, fetch=self.fetch)
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                promote({"results": []}, root, fetch=self.fetch)

    def test_unsupported_auth_requires_approval_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            unsupported = {**CARD, "securitySchemes": {"foo": {"type": "oauth2"}}}
            def fetch(url):
                return {CARD_URL: unsupported, PROOF_URL: self.proof}[url]
            result = promote(approved_feed(), Path(tmp), fetch=fetch)
            self.assertEqual(result["created"], [])

    def test_publisher_proof_is_not_equivalent_to_signed_package(self):
        record = {"verification": "card_read_only", "source_url": CARD_URL, "endpoint": API_URL}
        evidence = verify_publisher(record, CARD, fetch=self.fetch, now=self.now)
        self.assertEqual(evidence["method"], "https-origin-opt-in")
        self.assertEqual(evidence["host"], "weather.example.org")
        self.assertNotIn("signed", evidence)
        bad = dict(self.proof)
        bad["license"] = "NONE"
        self.proof = bad
        self.assertIsNone(verify_publisher(record, CARD, fetch=self.fetch, now=self.now))

if __name__ == "__main__":
    unittest.main()
