"""GitHub-backed cursor persistence: monotonic, bounded, no secrets."""
import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import crawl_checkpoint as checkpoints


KEY = "a" * 32
SHA = "b" * 40
DOC = {"version": 1, "erc8004": {
    KEY: {"next_block": 9000, "window": 512},
}}


class CheckpointPersistenceTests(unittest.TestCase):
    def test_numeric_only_versioned_schema(self):
        encoded = checkpoints.validate_state(DOC)
        self.assertEqual(json.loads(encoded), DOC)
        for bad in (
            {"version": 2, "erc8004": {}},
            {"version": True, "erc8004": {}},
            {"version": 1, "erc8004": [], "secret": "x"},
            {"version": 1, "erc8004": {"other": DOC["erc8004"][KEY]}},
            {"version": 1, "erc8004": {KEY: {"next_block": -1, "window": 1}}},
            {"version": 1, "erc8004": {KEY: {"next_block": True, "window": 1}}},
            {"version": 1, "erc8004": {KEY: {"next_block": 2, "window": 8192}}},
            {"version": 1, "erc8004": {KEY: {
                "next_block": 1, "window": 1, "rpc_token": "private"}}},
        ):
            with self.subTest(bad=bad), self.assertRaises(checkpoints.CheckpointError):
                checkpoints.validate_state(bad)

    def test_missing_checkpoint_loads_empty_without_initializing_branch(self):
        requests = []
        def api(method, path, payload=None):
            requests.append((method, path, payload))
            return None
        with patch.object(checkpoints, "github_api", api):
            state, sha = checkpoints.remote_checkpoint()
        self.assertEqual(state, {"version": 1, "erc8004": {}})
        self.assertIsNone(sha)
        self.assertEqual(requests, [
            ("GET", "/contents/state/erc8004.json?ref=crawler-state", None),
        ])

    def test_existing_checkpoint_decodes_from_contents_api(self):
        encoded = base64.b64encode(json.dumps(DOC).encode()).decode()
        with patch.object(checkpoints, "github_api",
                          return_value={"sha": SHA, "encoding": "base64",
                                        "content": encoded}):
            doc, sha = checkpoints.remote_checkpoint()
        self.assertEqual(doc, DOC)
        self.assertEqual(sha, SHA)

    def test_malformed_remote_blob_is_rejected_not_reset(self):
        with patch.object(checkpoints, "github_api",
                          return_value={"sha": SHA, "encoding": "base64",
                                        "content": base64.b64encode(
                                            b'{"erc8004":{"bad":"data"}}').decode()}):
            with self.assertRaises(checkpoints.CheckpointError):
                checkpoints.remote_checkpoint()

    def test_branch_creation_is_limited_to_exact_checkpoint_ref(self):
        actions = []
        def api(method, path, payload=None):
            actions.append((method, path, payload))
            if method == "GET" and path.endswith("/crawler-state"):
                return None
            if method == "GET" and path.endswith("/main"):
                return {"object": {"sha": SHA}}
            return {"ref": "refs/heads/crawler-state"}
        with patch.object(checkpoints, "github_api", api):
            checkpoints.ensure_state_branch()
        self.assertEqual(actions[-1], ("POST", "/git/refs",
                                      {"ref": "refs/heads/crawler-state", "sha": SHA}))

    def test_saving_merges_other_chain_cursors_and_uses_blob_sha(self):
        other = "c" * 32
        current = {"version": 1, "erc8004": {
            other: {"next_block": 80, "window": 32},
        }}
        writes = []
        def api(method, path, payload=None):
            if method == "PUT":
                writes.append((path, payload))
                return {"content": {"sha": SHA}}
            raise AssertionError("Only a write is expected after branch/read stubs")
        with patch.object(checkpoints, "ensure_state_branch"), \
             patch.object(checkpoints, "remote_checkpoint", return_value=(current, SHA)), \
             patch.object(checkpoints, "github_api", api):
            checkpoints.save_state(DOC)
        self.assertEqual(len(writes), 1)
        path, data = writes[0]
        self.assertEqual(path, "/contents/state/erc8004.json")
        self.assertEqual(data["branch"], "crawler-state")
        self.assertEqual(data["sha"], SHA)
        persisted = json.loads(base64.b64decode(data["content"]))
        self.assertEqual(persisted, {"version": 1, "erc8004": {
            other: {"next_block": 80, "window": 32},
            KEY: {"next_block": 9000, "window": 512},
        }})
        self.assertNotIn("Authorization", json.dumps(data))

    def test_stale_runner_cannot_overwrite_newer_cursor(self):
        current = {"version": 1, "erc8004": {
            KEY: {"next_block": 9001, "window": 128},
        }}
        with patch.object(checkpoints, "ensure_state_branch"), \
             patch.object(checkpoints, "remote_checkpoint", return_value=(current, SHA)), \
             patch.object(checkpoints, "github_api", side_effect=AssertionError):
            with self.assertRaisesRegex(checkpoints.CheckpointError, "stale"):
                checkpoints.save_state(DOC)

    def test_identical_checkpoint_never_creates_commit(self):
        with patch.object(checkpoints, "ensure_state_branch"), \
             patch.object(checkpoints, "remote_checkpoint", return_value=(DOC, SHA)), \
             patch.object(checkpoints, "github_api", side_effect=AssertionError):
            checkpoints.save_state(DOC)

    def test_github_token_and_repo_required_before_network_request(self):
        with patch.dict("os.environ", {"GITHUB_REPOSITORY": "bad/path/extra",
                                       "GH_TOKEN": "secret-value"}), \
             patch.object(checkpoints.urllib.request, "urlopen",
                          side_effect=AssertionError("Network should not run")):
            with self.assertRaises(checkpoints.CheckpointError):
                checkpoints.github_api("GET", "/contents/state/erc8004.json")
        with patch.dict("os.environ", {"GITHUB_REPOSITORY": "HereLiesAz/searcha2a",
                                       "GH_TOKEN": ""}), \
             patch.object(checkpoints.urllib.request, "urlopen",
                          side_effect=AssertionError("Network should not run")):
            with self.assertRaises(checkpoints.CheckpointError):
                checkpoints.github_api("GET", "/contents/state/erc8004.json")


if __name__ == "__main__":
    unittest.main()
