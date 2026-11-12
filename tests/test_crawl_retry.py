"""Public-only retry persistence and fixed-backoff health reporting."""
import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import crawl_retry as retry


CARD = "https://agent.example.org/.well-known/agent-card.json"
OTHER = "https://other.example.org/agent.json"
STATE = {"version": 1, "run_day": 0, "pending": []}
SHA = "a" * 40


def empty():
    return {"version": 1, "run_day": 0, "pending": []}


class CrawlRetryTests(unittest.TestCase):
    def test_only_explicit_public_https_card_urls_can_be_saved(self):
        for url in [
            "http://agent.example.org/card.json",
            "https://127.0.0.1/card.json",
            "https://localhost/card.json",
            "https://api.local/card.json",
            "https://secret.internal/card.json",
            "https://user:password@agent.example.org/card.json",
            "https://agent.example.org/card.json?token=secret",
            "https://agent.example.org/card.json#private",
            "file:///tmp/secrets", "https://agent.example.org:8443/card.json",
            "https://agent.example.org/" + "a" * 300,
        ]:
            with self.subTest(url=url):
                self.assertFalse(retry.safe_public_card_url(url))
        self.assertTrue(retry.safe_public_card_url(CARD))

    def test_accepted_state_is_small_versioned_and_forbids_private_origins(self):
        self.assertEqual(json.loads(retry.validate_retry_state(empty())), empty())
        sample = {"url": CARD, "source": "authorized-catalog",
                  "failures": 1, "retry_day": 1001}
        for bad in [
            {**empty(), "version": 2},
            {**empty(), "run_day": True},
            {**empty(), "pending": [sample]},
            {**empty(), "pending": [{**sample, "source": "mcp-resource"}]},
            {**empty(), "pending": [{**sample, "source": "agntcy-oci"}]},
            {**empty(), "pending": [{**sample, "source": ["registry"]}]},
            {**empty(), "pending": [{**sample, "source": "registry", "failures": False}]},
            {**empty(), "pending": [{**sample, "source": "registry", "token": "secret"}]},
            {**empty(), "pending": [{**sample, "source": "registry", "url": "https://agent.example.org/card?key=x"}]},
        ]:
            with self.subTest(state=bad), self.assertRaises(retry.RetryStateError):
                retry.validate_retry_state(bad)

    def test_transient_registry_failure_backoff_and_later_recovery(self):
        state = empty()
        result = retry.record_outcomes(state, [(CARD, "registry", "unavailable")], 1000)
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(state["pending"], [{
            "url": CARD, "source": "registry", "failures": 1, "retry_day": 1001,
        }])
        due, deferred = retry.due_retries(state, 1000)
        self.assertEqual(due, [])
        self.assertEqual(deferred, {CARD})
        due, deferred = retry.due_retries(state, 1001)
        self.assertEqual(due, [(CARD, "registry")])
        self.assertEqual(deferred, set())
        retry.record_outcomes(state, [(CARD, "registry", "unavailable")], 1001)
        self.assertEqual(state["pending"][0]["retry_day"], 1003)
        self.assertEqual(state["pending"][0]["failures"], 2)
        retry.record_outcomes(state, [(CARD, "registry", "accepted")], 1003)
        self.assertEqual(state["pending"], [])

    def test_nonrotating_domain_and_seed_failures_never_fill_the_retry_queue(self):
        state = empty()
        outcomes = [
            (f"https://host{i}.example.org/card.json", "domain-probe", "unavailable")
            for i in range(200)
        ] + [(CARD, "seed", "unavailable")]
        stats = retry.record_outcomes(state, outcomes, 1000)
        self.assertEqual(stats["attempted"], 201)
        self.assertEqual(stats["unavailable"], 201)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["by_source"]["seed"]["unavailable"], 1)

    def test_private_failures_are_not_reported_or_queued(self):
        state = empty()
        metrics = retry.record_outcomes(state, [
            ("https://private-team.example.org/card.json", "authorized-catalog", "unavailable"),
            ("https://private-team.example.org/card.json", "mcp-resource", "unavailable"),
            (CARD, "registry", "accepted"),
        ], 1000)
        self.assertEqual(state["pending"], [])
        self.assertEqual(metrics["attempted"], 1)
        self.assertEqual(set(metrics["by_source"]), {"registry"})
        self.assertNotIn("private-team", json.dumps(metrics))

    def test_overdue_queue_is_deterministically_capped(self):
        state = {"version": 1, "run_day": 1000, "pending": [
            {"url": f"https://agent{i:02d}.example.org/card.json", "source": "registry",
             "failures": 2, "retry_day": 1001} for i in range(20)
        ]}
        due, skipped = retry.due_retries(state, 1001)
        self.assertEqual(len(due), 16)
        self.assertEqual(due[0][0], "https://agent00.example.org/card.json")
        self.assertEqual(due[-1][0], "https://agent15.example.org/card.json")
        self.assertEqual(len(skipped), 4)

    def test_queue_full_keeps_chain_retries_and_reports_eviction(self):
        state = empty()
        for i in range(retry.MAX_PENDING):
            retry.record_outcomes(state, [
                (f"https://agent{i:02d}.example.org/card.json", "registry", "unavailable"),
            ], 1000)
        metrics = retry.record_outcomes(state, [
            (OTHER, "erc8004-registration", "unavailable")], 1001)
        self.assertEqual(metrics["queue_full"], 1)
        self.assertEqual(len(state["pending"]), retry.MAX_PENDING)
        self.assertTrue(any(item["url"] == OTHER and
                            item["source"] == "erc8004-registration"
                            for item in state["pending"]))
        metrics = retry.record_outcomes(state, [
            (CARD, "public-directory", "unavailable")], 1001)
        self.assertEqual(metrics["queue_full"], 1)
        self.assertEqual(len(state["pending"]), retry.MAX_PENDING)

    def test_due_retries_prioritize_onchain_and_registry_over_github(self):
        state = {"version": 1, "run_day": 1000, "pending": [
            {"url": f"https://github{i:02d}.example.org/card.json",
             "source": "github", "failures": 1, "retry_day": 1001}
            for i in range(18)
        ] + [
            {"url": CARD, "source": "erc8004-registration",
             "failures": 1, "retry_day": 1001},
            {"url": OTHER, "source": "registry",
             "failures": 1, "retry_day": 1001},
        ]}
        due, deferred = retry.due_retries(state, 1001)
        self.assertEqual(due[0], (CARD, "erc8004-registration"))
        self.assertEqual(due[1], (OTHER, "registry"))
        self.assertEqual(len(due), 16)
        self.assertEqual(len(deferred), 4)

    def test_old_queue_full_of_github_is_compacted_without_leaking_urls(self):
        state = {"version": 1, "run_day": 1000, "pending": [
            {"url": f"https://github{i:02d}.example.org/card.json",
             "source": "github", "failures": 1, "retry_day": 1001}
            for i in range(39)
        ] + [{"url": CARD, "source": "registry",
              "failures": 1, "retry_day": 1001}]}
        metrics = retry.record_outcomes(state, [], 1001)
        self.assertEqual(metrics["queue_full"], 29)
        self.assertEqual(metrics["pending"], 11)
        self.assertEqual(len([e for e in state["pending"]
                              if e["source"] == "github"]), 10)
        self.assertTrue(any(e["url"] == CARD for e in state["pending"]))
        self.assertEqual(state["run_day"], 1001)

    def test_chain_registration_displaces_lower_priority_at_capacity(self):
        state = {"version": 1, "run_day": 1000, "pending": [
            {"url": f"https://registry{i:02d}.example.org/card.json",
             "source": "registry", "failures": 1, "retry_day": 1001}
            for i in range(40)
        ]}
        metrics = retry.record_outcomes(
            state, [(CARD, "erc8004-registration", "unavailable")], 1001)
        self.assertEqual(len(state["pending"]), 40)
        self.assertEqual(metrics["queue_full"], 1)
        self.assertTrue(any(e["url"] == CARD and
                            e["source"] == "erc8004-registration"
                            for e in state["pending"]))
        self.assertFalse(any(e["url"] ==
                             "https://registry39.example.org/card.json"
                             for e in state["pending"]))

    def test_github_can_never_consume_all_public_retry_slots(self):
        state = empty()
        for i in range(40):
            retry.record_outcomes(state, [(
                f"https://github{i:02d}.example.org/card.json",
                "github", "unavailable")], 1000)
        self.assertEqual(len(state["pending"]), 10)
        retry.record_outcomes(state, [(CARD, "registry", "unavailable")], 1000)
        self.assertEqual(len(state["pending"]), 11)
        self.assertEqual(state["pending"][0]["url"], CARD)

    def test_no_lower_priority_entry_means_unpersistable_chain_fails_closed(self):
        state = {"version": 1, "run_day": 1000, "pending": [
            {"url": f"https://chain{i:02d}.example.org/card.json",
             "source": "erc8004-registration", "failures": 1, "retry_day": 1001}
            for i in range(40)
        ]}
        original = json.loads(json.dumps(state))
        with self.assertRaisesRegex(retry.RetryStateError, "Unpersisted chain"):
            retry.record_outcomes(state, [(CARD, "erc8004-registration",
                                           "unavailable")], 1001)
        self.assertEqual(state, original)

    def test_existing_registry_retry_is_promoted_to_chain_provenance(self):
        state = empty()
        retry.record_outcomes(state, [(CARD, "registry", "unavailable")], 1000)
        retry.record_outcomes(state, [
            (CARD, "erc8004-registration", "unavailable")], 1001)
        self.assertEqual(state["pending"], [{
            "url": CARD, "source": "erc8004-registration",
            "failures": 2, "retry_day": 1003,
        }])

    def test_monotonic_date_and_backoff_bounds(self):
        state = empty()
        for day in range(1000, 1010):
            retry.record_outcomes(state, [(CARD, "github", "unavailable")], day)
        self.assertEqual(state["pending"][0]["failures"], 8)
        self.assertEqual(state["pending"][0]["retry_day"], 1039)
        with self.assertRaises(retry.RetryStateError):
            retry.record_outcomes(state, [], 1008)

    def test_load_missing_file_defaults_to_empty_state(self):
        with patch.object(retry.crawl_checkpoint, "github_api", return_value=None) as api:
            state, sha = retry.remote_retry_state()
        self.assertEqual((state, sha), (empty(), None))
        self.assertEqual(api.call_args.args, (
            "GET", "/contents/state/public-card-retries.json?ref=crawler-state"))

    def test_load_existing_state_checks_base64_blob_and_git_sha(self):
        fixture = {"version": 1, "run_day": 1000, "pending": [{
            "url": CARD, "source": "registry", "failures": 1, "retry_day": 1001,
        }]}
        contents = base64.b64encode(retry.validate_retry_state(fixture)).decode("ascii")
        with patch.object(retry.crawl_checkpoint, "github_api",
                          return_value={"sha": SHA, "encoding": "base64",
                                        "content": contents}):
            loaded, sha = retry.remote_retry_state()
        self.assertEqual(loaded, fixture)
        self.assertEqual(sha, SHA)

    def test_save_is_optimistic_and_rejects_stale_runner(self):
        old = {"version": 1, "run_day": 1002, "pending": []}
        with patch.object(retry.crawl_checkpoint, "ensure_state_branch"), \
             patch.object(retry, "remote_retry_state", return_value=(old, SHA)), \
             patch.object(retry.crawl_checkpoint, "github_api",
                          side_effect=AssertionError("Should not overwrite")):
            with self.assertRaisesRegex(retry.RetryStateError, "stale"):
                retry.save_retry_state({"version": 1, "run_day": 1001, "pending": []})

    def test_save_uses_existing_blob_sha_for_atomic_update(self):
        stage = {"version": 1, "run_day": 1001, "pending": [{
            "url": CARD, "source": "registry", "failures": 1, "retry_day": 1002,
        }]}
        calls = []
        def api(method, path, payload=None):
            calls.append((method, path, payload))
            return {"content": {"sha": SHA}}
        with patch.object(retry.crawl_checkpoint, "ensure_state_branch"), \
             patch.object(retry, "remote_retry_state", return_value=(empty(), SHA)), \
             patch.object(retry.crawl_checkpoint, "github_api", api):
            retry.save_retry_state(stage)
        self.assertEqual(len(calls), 1)
        method, path, payload = calls[0]
        self.assertEqual((method, path), ("PUT", "/contents/state/public-card-retries.json"))
        self.assertEqual(payload["sha"], SHA)
        self.assertEqual(payload["branch"], "crawler-state")
        self.assertEqual(json.loads(base64.b64decode(payload["content"])), stage)

    def test_workflow_persists_queue_before_advancing_chain_cursor(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    ".github/workflows/pages.yml").read_text(encoding="utf-8")
        steps = (
            "Load public Agent Card retry state",
            "Build public catalog",
            "Ingest scheduled crawl into persistent registry",
            "Persist public Agent Card retries",
            "Commit durable ERC-8004 checkpoint",
        )
        positions = [workflow.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("--retry-in .crawl-state/public-retries.json", workflow)
        self.assertIn("--health-output dist/api/v1/crawler-health.json", workflow)


if __name__ == "__main__":
    unittest.main()
