"""Adversarial tests for scheduled agent endpoint reachability checks."""
import io
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import agent_health


class FakeResponse(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class HealthTests(unittest.TestCase):
    def test_pinned_public_https_head_without_task_body_or_redirect(self):
        item = {"id": "a" * 20, "endpoint": "https://example.org/a2a"}
        opened = []

        class Connection:
            def __init__(self, host, address):
                opened.append((host, address))

            def request(self, method, path, headers):
                self.method, self.path, self.headers = method, path, headers
                self_ref[0] = self

            def getresponse(self):
                return type("Response", (), {"status": 405})()

            def close(self):
                pass

        self_ref = [None]
        with patch.object(agent_health, "require_public_https"), \
             patch.object(agent_health.socket, "getaddrinfo", return_value=[
                 (2, 1, 6, "", ("8.8.8.8", 443))]), \
             patch.object(agent_health, "PinnedHTTPSConnection", Connection):
            result = agent_health.probe(item)
        self.assertEqual(result["status"], "responding")
        self.assertEqual(result["http_status"], 405)
        self.assertEqual(opened, [("example.org", "8.8.8.8")])
        self.assertEqual(self_ref[0].method, "HEAD")
        self.assertEqual(self_ref[0].path, "/a2a")
        self.assertNotIn("Authorization", self_ref[0].headers)

    def test_never_contacts_private_destination_or_tokenized_endpoint(self):
        item = {"id": "a" * 20, "endpoint": "https://example.org/a2a"}
        with patch.object(agent_health, "require_public_https"), \
             patch.object(agent_health.socket, "getaddrinfo", return_value=[
                 (2, 1, 6, "", ("127.0.0.1", 443))]), \
             patch.object(agent_health, "PinnedHTTPSConnection") as connection:
            self.assertEqual(agent_health.probe(item)["status"], "skipped")
            connection.assert_not_called()
        with patch.object(agent_health, "require_public_https") as valid:
            self.assertEqual(agent_health.probe({
                **item, "endpoint": item["endpoint"] + "?token=secret",
            })["status"], "skipped")
            valid.assert_not_called()

    def test_failed_probe_is_not_a_verified_protocol(self):
        with patch.object(agent_health, "require_public_https",
                          side_effect=agent_health.DiscoveryError("blocked")):
            self.assertEqual(agent_health.probe({
                "id": "a" * 20, "endpoint": "https://blocked.example/a2a",
            })["status"], "skipped")
        with patch.object(agent_health, "require_public_https"), \
             patch.object(agent_health.socket, "getaddrinfo",
                          side_effect=OSError("offline")):
            self.assertEqual(agent_health.probe({
                "id": "a" * 20, "endpoint": "https://offline.example/a2a",
            })["status"], "unreachable")

    def test_bounded_due_list_and_authenticated_upload(self):
        observed = []

        def opener(req, timeout):
            observed.append((req.full_url, req.get_method(),
                             req.get_header("Authorization"),
                             json.loads(req.data) if req.data else None))
            if req.get_method() == "GET":
                return FakeResponse({"entries": [
                    {"id": "a" * 20, "endpoint": "https://example.org/a2a"},
                ]})
            return FakeResponse({"status": "stored", "stored": 1})

        fake_probe = lambda item: {
            **item, "status": "unreachable", "http_status": None,
        }
        self.assertEqual(agent_health.run("https://api.example", "secret",
                                         opener=opener, probe_fn=fake_probe),
                         {"attempted": 1, "stored": 1})
        self.assertEqual(observed[0][1], "GET")
        self.assertEqual(observed[1][1], "POST")
        self.assertTrue(all(row[2] == "Bearer secret" for row in observed))
        self.assertEqual(observed[1][3]["results"][0]["status"], "unreachable")

    def test_rejects_oversized_due_list_before_any_probe(self):
        def opener(req, timeout):
            return FakeResponse({"entries": [
                {"id": "a" * 20, "endpoint": "https://example.org/a2a"},
            ] * 61})
        with self.assertRaises(ValueError):
            agent_health.run("https://api.example", "secret", opener=opener,
                             probe_fn=lambda _: self.fail("Unexpected probe"))


if __name__ == "__main__":
    unittest.main()
