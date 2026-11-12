"""Durable ERC-8004 cursors advance only through complete confirmed windows."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discover

CONTRACT = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
RPC = "https://rpc.example.org/reads"
SOURCE = {"enabled": True, "registry_address": CONTRACT, "rpc_url": RPC,
          "start_block": 100, "finality_blocks": 0, "window_blocks": 100}
EVENT = {
    "address": CONTRACT,
    "topics": [
        "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a",
        "0x" + "0" * 63 + "2",
        "0x" + "0" * 24 + "f" * 40,
    ],
    "data": (
        "0x0000000000000000000000000000000000000000000000000000000000000020"
        "000000000000000000000000000000000000000000000000000000000000002b"
        "68747470733a2f2f6167656e742e6578616d706c652e6e65742f726567697374"
        "726174696f6e2e6a736f6e000000000000000000000000000000000000000000"
    ),
}
URL = "https://agent.example.net/registration.json"


def config(**override):
    return {"erc8004_rpc_sources": [{**SOURCE, **override}]}


def cursor(state):
    return next(iter(state["erc8004"].values()))


class ERC8004CheckpointTests(unittest.TestCase):
    def test_bootstrap_from_configured_historical_block_without_gaps(self):
        seen = []
        def rpc(url, method, args):
            self.assertEqual(url, RPC)
            if method == "eth_blockNumber":
                return {"result": "0x12c"}  # 300
            self.assertEqual(method, "eth_getLogs")
            begin = int(args[0]["fromBlock"], 16)
            end = int(args[0]["toBlock"], 16)
            seen.append((begin, end))
            return {"result": [EVENT] if begin == 100 else []}
        state = {"version": 1, "erc8004": {}}
        urls, warnings = discover.erc8004_checkpointed_sources(
            config(), state, post=rpc)
        self.assertEqual(urls, [(URL, "erc8004-registration")])
        self.assertEqual(seen, [(100, 199), (200, 299), (300, 300)])
        self.assertEqual(cursor(state), {"next_block": 301, "window": 100})
        self.assertEqual(warnings, [])

    def test_second_run_resumes_exactly_where_first_finished(self):
        state = {"version": 1, "erc8004": {}}
        seen = []
        def rpc(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x12c"}  # 300
            seen.append((int(params[0]["fromBlock"], 16),
                         int(params[0]["toBlock"], 16)))
            return {"result": []}
        discover.erc8004_checkpointed_sources(config(), state, post=rpc)
        seen.clear()
        def later(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x15e"}  # 350
            seen.append((int(params[0]["fromBlock"], 16),
                         int(params[0]["toBlock"], 16)))
            return {"result": []}
        urls, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=0), state, post=later)
        self.assertEqual(seen, [(301, 350)])
        self.assertEqual(cursor(state)["next_block"], 351)
        self.assertEqual(urls, [])
        self.assertEqual(warnings, [])

    def test_first_run_uses_finality_and_bounded_recent_lookback(self):
        calls = []
        def rpc(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x1000"}  # 4096
            calls.append((int(params[0]["fromBlock"], 16),
                          int(params[0]["toBlock"], 16)))
            return {"result": []}
        state = {"version": 1, "erc8004": {}}
        discover.erc8004_checkpointed_sources(config(
            start_block=None, finality_blocks=64, lookback_blocks=512,
            window_blocks=1024), state, post=rpc)
        self.assertEqual(calls, [(3521, 4032)])
        self.assertEqual(cursor(state)["next_block"], 4033)

    def test_overfull_block_range_is_retried_with_smaller_windows(self):
        seen = []
        def rpc(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x40"}  # 64
            begin = int(params[0]["fromBlock"], 16)
            end = int(params[0]["toBlock"], 16)
            seen.append((begin, end))
            return {"result": [EVENT] * 121 if end - begin >= 64 else []}
        state = {"version": 1, "erc8004": {}}
        urls, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=0, window_blocks=128), state, post=rpc)
        self.assertEqual(seen, [(0, 64), (0, 63), (64, 64)])
        self.assertEqual(cursor(state)["next_block"], 65)
        self.assertEqual(urls, [])
        self.assertEqual(warnings, [])

    def test_overfull_single_block_never_advances_cursor(self):
        requests = []
        def rpc(_, method, args):
            if method == "eth_blockNumber":
                return {"result": "0x0"}
            requests.append(args)
            return {"result": [EVENT] * 121}
        state = {"version": 1, "erc8004": {}}
        urls, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=0, window_blocks=1), state, post=rpc)
        self.assertEqual(len(requests), 1)
        self.assertEqual(urls, [])
        self.assertEqual(cursor(state)["next_block"], 0)
        self.assertIn("ERC-8004 single block exceeds bounded log budget", warnings)

    def test_rpc_failure_retries_without_advancing_prior_checkpoint(self):
        state = {"version": 1, "erc8004": {}}
        def good(_, method, __):
            return {"result": "0x14"} if method == "eth_blockNumber" else {"result": []}
        discover.erc8004_checkpointed_sources(config(start_block=20), state, post=good)
        prior = dict(cursor(state))
        def bad(_, method, __):
            if method == "eth_blockNumber":
                return {"result": "0x1e"}
            raise discover.DiscoveryError("Network secret")
        urls, errors = discover.erc8004_checkpointed_sources(
            config(start_block=0), state, post=bad)
        self.assertEqual(urls, [])
        self.assertEqual(cursor(state), prior)
        self.assertEqual(errors, ["ERC-8004 source unavailable"])
        self.assertNotIn("secret", str(errors))

    def test_fixed_manual_backfill_does_not_mutate_incremental_cursor(self):
        state = {"version": 1, "erc8004": {}}
        def rpc(_, method, params):
            self.assertEqual(method, "eth_getLogs")
            self.assertEqual(params[0]["fromBlock"], "0x2")
            self.assertEqual(params[0]["toBlock"], "0x4")
            return {"result": [EVENT]}
        urls, warnings = discover.erc8004_checkpointed_sources(
            config(from_block=2, to_block=4), state, post=rpc)
        self.assertEqual(urls, [(URL, "erc8004-registration")])
        self.assertEqual(warnings, [])
        self.assertEqual(state["erc8004"], {})

    def test_invalid_cursor_stops_before_rpc(self):
        # Precomputed independently for the fixed RPC locator and contract.
        state = {"version": 1, "erc8004": {
            "4d50994a8bbdcb94798d8d08e05fe4ba":
                {"next_block": "100", "window": 10},
        }}
        with self.assertRaises(discover.DiscoveryError):
            discover.erc8004_checkpointed_sources(
                config(), state,
                post=lambda *_: (_ for _ in ()).throw(AssertionError()))

    def test_exactly_full_log_budget_does_not_misclassify_complete_range(self):
        seen = []
        def rpc(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x1"}
            seen.append((int(params[0]["fromBlock"], 16),
                         int(params[0]["toBlock"], 16)))
            return {"result": [EVENT] * 120 if len(seen) == 1 else []}
        state = {"version": 1, "erc8004": {}}
        cards, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=0, window_blocks=2), state, post=rpc)
        self.assertEqual(seen, [(0, 1), (0, 0), (1, 1)])
        self.assertEqual(cursor(state)["next_block"], 2)
        self.assertEqual(cards, [])
        self.assertEqual(warnings, [])

    def test_provider_block_range_error_shrinks_then_retries(self):
        seen = []
        def rpc(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x8"}
            begin = int(params[0]["fromBlock"], 16)
            end = int(params[0]["toBlock"], 16)
            seen.append((begin, end))
            if end - begin >= 8:
                raise discover.DiscoveryError("provider refuses wide range")
            return {"result": []}
        state = {"version": 1, "erc8004": {}}
        cards, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=0, window_blocks=16), state, post=rpc)
        self.assertEqual(seen, [(0, 8), (0, 7), (8, 8)])
        self.assertEqual(cursor(state)["next_block"], 9)
        self.assertEqual(cards, [])
        self.assertEqual(warnings, [])

    def test_provider_restriction_keeps_adapted_span_after_success(self):
        seen = []
        rejected = []
        def rpc(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x3e8"}  # 1000
            first = int(params[0]["fromBlock"], 16)
            last = int(params[0]["toBlock"], 16)
            seen.append((first, last))
            if last - first + 1 > 128:
                rejected.append((first, last))
                raise discover.DiscoveryError("range too large")
            return {"result": []}
        state = {"version": 1, "erc8004": {}}
        cards, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=0, window_blocks=4096), state, post=rpc)
        self.assertEqual(cards, [])
        self.assertEqual(warnings, [])
        self.assertEqual(cursor(state)["next_block"], 1001)
        self.assertEqual(len(rejected), 5)
        self.assertEqual(seen[5], (0, 127))
        self.assertEqual(seen[-1], (896, 1000))
        self.assertEqual(len(seen), 13)

    def test_failed_bootstrap_keeps_initial_height_across_new_chain_tips(self):
        state = {"version": 1, "erc8004": {}}
        def failure(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x3e8"}  # 1000
            raise discover.DiscoveryError("RPC range unavailable")
        found, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=None, finality_blocks=0,
                   lookback_blocks=512, window_blocks=512),
            state, post=failure)
        self.assertEqual(found, [])
        self.assertEqual(warnings, ["ERC-8004 source unavailable"])
        self.assertEqual(cursor(state)["next_block"], 489)

        first_requested = []
        def recovery(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x7d0"}  # 2000
            first_requested.append(int(params[0]["fromBlock"], 16))
            return {"result": []}
        found, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=None, finality_blocks=0,
                   lookback_blocks=512, window_blocks=512),
            state, post=recovery)
        self.assertEqual(first_requested[0], 489)
        self.assertEqual(cursor(state)["next_block"], 2001)
        self.assertEqual(found, [])
        self.assertEqual(warnings, [])

    def test_budget_reduced_window_recovers_on_next_quiet_run(self):
        state = {"version": 1, "erc8004": {
            "4d50994a8bbdcb94798d8d08e05fe4ba":
                {"next_block": 40, "window": 1},
        }}
        requested = []
        def rpc(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x186a0"}  # 100000
            requested.append((int(params[0]["fromBlock"], 16),
                              int(params[0]["toBlock"], 16)))
            return {"result": []}
        found, warnings = discover.erc8004_checkpointed_sources(
            config(start_block=0, window_blocks=4096), state, post=rpc)
        self.assertEqual(found, [])
        self.assertEqual(requested[0], (40, 40))
        self.assertEqual(requested[1], (41, 42))
        self.assertEqual(cursor(state)["window"], 4096)
        self.assertGreater(cursor(state)["next_block"], 10000)
        self.assertEqual(len(requested), 16)
        self.assertEqual(warnings, ["ERC-8004 backfill continues at saved cursor"])

    def test_partial_window_budget_is_bounded_with_retriable_height(self):
        called = []
        def rpc(_, method, params):
            if method == "eth_blockNumber":
                return {"result": "0x186a0"}  # 100000
            called.append(params)
            return {"result": []}
        state = {"version": 1, "erc8004": {}}
        _, errors = discover.erc8004_checkpointed_sources(
            config(start_block=0, window_blocks=4096), state, post=rpc)
        self.assertEqual(len(called), 16)
        self.assertEqual(cursor(state)["next_block"], 65536)
        self.assertEqual(errors, ["ERC-8004 backfill continues at saved cursor"])

    def test_chain_progress_not_exposed_as_claim_of_verification(self):
        def rpc(_, method, params):
            return {"result": "0x64"} if method == "eth_blockNumber" else {"result": [EVENT]}
        state = {"version": 1, "erc8004": {}}
        urls, errors = discover.erc8004_checkpointed_sources(
            config(start_block=100), state, post=rpc)
        self.assertEqual(urls, [(URL, "erc8004-registration")])
        self.assertEqual(errors, [])
        self.assertEqual(set(cursor(state)), {"next_block", "window"})
        self.assertNotIn("verified", str(state))


if __name__ == "__main__":
    unittest.main()
