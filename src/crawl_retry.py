#!/usr/bin/env python3
"""Persistent retries for public Agent Card URL leads, with privacy-safe health.

Only explicitly public discovery origins can enter this state. Private catalogs,
MCP/OCI credentials, and their linked card URLs are never persisted here.
The state branch is public; URLs in this queue must be public metadata already.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import urllib.parse

import crawl_checkpoint

STATE_PATH = "state/public-card-retries.json"
MAX_STATE_BYTES = 16000
MAX_PENDING = 40
MAX_RETRIES_PER_CRAWL = 16
MAX_URL_LENGTH = 280
MAX_FAILURES = 8
MAX_DAY = 3000000
PUBLIC_ORIGINS = frozenset({
    "seed", "domain-probe", "webfinger", "dns-nais",
    "registry", "github", "erc8004-registration",
    "public-directory", "manifest",
})
STATUSES = frozenset({"accepted", "invalid", "unavailable"})
RETRYABLE_ORIGINS = frozenset({
    "registry", "github", "erc8004-registration", "public-directory", "manifest",
})
EMPTY_STATE = {"version": 1, "run_day": 0, "pending": []}


class RetryStateError(Exception):
    """Reject invalid or unsafe public retry state."""


def safe_public_card_url(url):
    """Static privacy filter; network-time DNS protections remain in fetch_json."""
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host or parsed.username or
                parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or parsed.path.startswith("//")
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in url)
                or not re.fullmatch(r"[a-z0-9.-]+", host, flags=re.I)
                or "." not in host or host.lower().endswith(
                    (".local", ".localhost", ".internal", ".test"))):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            return True
    except ValueError:
        return False


def validate_retry_state(state):
    """Validate strict, small, public-only JSON, with no private-source entries."""
    if (not isinstance(state, dict)
            or set(state) != {"version", "run_day", "pending"}
            or type(state.get("version")) is not int or state["version"] != 1
            or type(state.get("run_day")) is not int
            or not 0 <= state["run_day"] <= MAX_DAY
            or not isinstance(state.get("pending"), list)
            or len(state["pending"]) > MAX_PENDING):
        raise RetryStateError("Invalid public retry state")
    seen = set()
    for entry in state["pending"]:
        if (not isinstance(entry, dict)
                or set(entry) != {"url", "source", "failures", "retry_day"}
                or not safe_public_card_url(entry["url"])
                or not isinstance(entry["source"], str)
                or entry["source"] not in RETRYABLE_ORIGINS
                or type(entry["failures"]) is not int
                or not 1 <= entry["failures"] <= MAX_FAILURES
                or type(entry["retry_day"]) is not int
                or not 0 <= entry["retry_day"] <= MAX_DAY
                or entry["url"] in seen):
            raise RetryStateError("Invalid public retry entry")
        seen.add(entry["url"])
    raw = (json.dumps(state, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_STATE_BYTES:
        raise RetryStateError("Oversized retry state")
    return raw


def due_retries(state, today):
    """Prioritize overdue public URLs; defer every other queued URL."""
    validate_retry_state(state)
    if type(today) is not int or not 1 <= today <= MAX_DAY:
        raise RetryStateError("Invalid retry date")
    ready = sorted((entry for entry in state["pending"]
                    if entry["retry_day"] <= today),
                   key=lambda x: (x["retry_day"], x["url"]))
    chosen = ready[:MAX_RETRIES_PER_CRAWL]
    deferred = {entry["url"] for entry in state["pending"]} - {
        entry["url"] for entry in chosen}
    return [(entry["url"], entry["source"]) for entry in chosen], deferred


def record_outcomes(state, outcomes, today):
    """Apply the confirmed crawl outcomes, returning public aggregate statistics.

    Each tuple is (public card URL, public origin category, outcome); a valid
    JSON document without Agent Card links is INVALID, never a live agent.
    """
    validate_retry_state(state)
    if type(today) is not int or not 1 <= today <= MAX_DAY:
        raise RetryStateError("Invalid retry date")
    if today < state["run_day"]:
        raise RetryStateError("Refusing older retry state")
    previous = {e["url"]: dict(e) for e in state["pending"]}
    by_source = {}
    total = {"attempted": 0, "accepted": 0, "invalid": 0,
             "unavailable": 0, "queue_full": 0}
    for url, origin, status in outcomes:
        if origin not in PUBLIC_ORIGINS or not safe_public_card_url(url):
            continue
        if status not in STATUSES:
            raise RetryStateError("Unexpected crawl outcome")
        counters = by_source.setdefault(origin, {
            "attempted": 0, "accepted": 0, "invalid": 0, "unavailable": 0})
        counters["attempted"] += 1
        counters[status] += 1
        total["attempted"] += 1
        total[status] += 1
        if status != "unavailable":
            previous.pop(url, None)
            continue
        if origin not in RETRYABLE_ORIGINS:
            # Reprobed daily: retaining domain-path 404s would crowd out
            # rotating registries and transient on-chain registration links.
            continue
        old = previous.get(url)
        failures = min(MAX_FAILURES, 1 + (old["failures"] if old else 0))
        if old is None and len(previous) >= MAX_PENDING:
            if origin == "erc8004-registration":
                # Keep a confirmed block window retriable if chain URLs cannot
                # be saved. This error aborts the build before cursor commit.
                raise RetryStateError("Unpersisted chain registration URLs")
            total["queue_full"] += 1
            continue
        wait_days = min(30, 2 ** (failures - 1))
        previous[url] = {
            "url": url, "source": old["source"] if old else origin,
            "failures": failures, "retry_day": min(MAX_DAY, today + wait_days),
        }
    state["pending"] = sorted(previous.values(), key=lambda x: x["url"])
    state["run_day"] = today
    validate_retry_state(state)
    return {**total, "pending": len(state["pending"]),
            "by_source": dict(sorted(by_source.items()))}


def remote_retry_state():
    """Load the public queue from the existing isolated crawler-state branch."""
    info = crawl_checkpoint.github_api(
        "GET", "/contents/" + STATE_PATH + "?ref=" + crawl_checkpoint.STATE_BRANCH)
    if info is None:
        return {"version": 1, "run_day": 0, "pending": []}, None
    if not isinstance(info, dict) or info.get("encoding") != "base64":
        raise RetryStateError("Unexpected retry state response")
    sha, data = info.get("sha"), info.get("content")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or not isinstance(data, str):
        raise RetryStateError("Malformed retry state blob")
    try:
        raw = base64.b64decode(data)
        if len(raw) > MAX_STATE_BYTES:
            raise RetryStateError("Oversized retry state blob")
        state = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RetryStateError("Invalid retry state JSON") from exc
    validate_retry_state(state)
    return state, sha


def save_retry_state(staged):
    """Optimistic, monotonic save after successful authenticated registry ingest."""
    validate_retry_state(staged)
    crawl_checkpoint.ensure_state_branch()
    current, sha = remote_retry_state()
    if staged["run_day"] < current["run_day"]:
        raise RetryStateError("Refusing stale retry state")
    if current == staged:
        return
    payload = {
        "branch": crawl_checkpoint.STATE_BRANCH,
        "message": "chore(crawler): persist public card retries [skip ci]",
        "content": base64.b64encode(validate_retry_state(staged)).decode(),
    }
    if sha:
        payload["sha"] = sha
    crawl_checkpoint.github_api("PUT", "/contents/" + STATE_PATH, payload)


def main():
    parser = argparse.ArgumentParser(description="Load/save public card retry state")
    parser.add_argument("action", choices=("load", "save"))
    parser.add_argument("path")
    args = parser.parse_args()
    target = Path(args.path)
    if args.action == "load":
        state, _ = remote_retry_state()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(validate_retry_state(state))
    else:
        try:
            state = json.loads(target.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SystemExit("Public retry state unavailable") from exc
        save_retry_state(state)
    print("Public card retry state " + ("loaded" if args.action == "load" else "saved"))


if __name__ == "__main__":
    main()
