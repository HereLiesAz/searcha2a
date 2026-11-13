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

    def _probe_with(self, code, headers=None):
        class Connection:
            def __init__(self, host, address):
                self.sock = None

            def request(self, method, path, headers):
                pass

            def getresponse(self):
                return type("Response", (), {
                    "status": code,
                    "getheader": staticmethod(lambda name, default=None: (headers or {}).get(name, default)),
                })()

            def close(self):
                pass

        with patch.object(agent_health, "require_public_https"), \
             patch.object(agent_health.socket, "getaddrinfo", return_value=[
                 (2, 1, 6, "", ("8.8.8.8", 443))]), \
             patch.object(agent_health, "PinnedHTTPSConnection", Connection):
            return agent_health.probe({"id": "a" * 20, "endpoint": "https://example.org/a2a"})

    def test_honest_taxonomy_for_http_codes(self):
        expected = {200: ("responding", "reachable"), 204: ("responding", "reachable"),
                    400: ("responding", "reachable"), 401: ("responding", "reachable"),
                    405: ("responding", "reachable"), 429: ("responding", "reachable"),
                    404: ("path_not_found", "path_not_found"),
                    410: ("path_not_found", "path_not_found"),
                    418: ("unexpected_status", "unexpected_status"),
                    500: ("server_error", "server_error"),
                    502: ("server_error", "server_error")}
        for code, (status, state) in expected.items():
            result = self._probe_with(code)
            self.assertEqual((result["status"], result["state"]), (status, state), code)
            self.assertEqual(result["http_status"], code)

    def test_404_is_never_called_responding(self):
        self.assertNotEqual(self._probe_with(404)["status"], "responding")

    def test_redirect_with_valid_https_target_counts_but_is_flagged_and_not_followed(self):
        result = self._probe_with(302, {"Location": "https://example.org/v2/a2a"})
        self.assertEqual((result["status"], result["state"]), ("responding", "reachable"))
        self.assertIs(result["redirect"], True)

    def test_redirect_without_valid_target_is_redirected_not_reachable(self):
        for location in (None, "http://example.org/a2a", "https://someone@example.org/",
                         "https://127.0.0.1/x", "https://10.0.0.5/x", "javascript:alert(1)"):
            result = self._probe_with(308, {"Location": location} if location else {})
            self.assertEqual((result["status"], result["state"]), ("redirected", "redirected"), location)

    def test_probe_records_only_a_challenge_kind_not_header_values(self):
        result = self._probe_with(401, {"WWW-Authenticate": 'Bearer realm="private-realm"'})
        self.assertEqual(result["challenge"], "bearer")
        self.assertNotIn("private-realm", json.dumps(result))

    def test_failed_probes_carry_matching_state(self):
        with patch.object(agent_health, "require_public_https"), \
             patch.object(agent_health.socket, "getaddrinfo", side_effect=OSError("offline")):
            result = agent_health.probe({"id": "a" * 20, "endpoint": "https://o.example/a2a"})
        self.assertEqual((result["status"], result["state"]), ("unreachable", "unreachable"))

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
