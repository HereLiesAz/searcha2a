#!/usr/bin/env python3
"""Durable ERC-8004 crawl cursors, stored outside the public Pages build.

The GitHub Actions publication job loads the last committed cursor before
discovery and publishes the new cursor only after authenticated registry ingest.
The checkpoint branch contains numeric positions, never API keys or agent URIs.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

STATE_BRANCH = "crawler-state"
STATE_PATH = "state/erc8004.json"
MAX_STATE_BYTES = 16384
EMPTY_STATE = {"version": 1, "erc8004": {}}


class CheckpointError(Exception):
    """An unsafe checkpoint or failed state persistence operation."""


def validate_state(value):
    """Require a small, non-secret, versioned cursor document."""
    if (not isinstance(value, dict) or set(value) != {"version", "erc8004"}
            or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("erc8004"), dict)
            or len(value["erc8004"]) > 32):
        raise CheckpointError("Unsupported checkpoint schema")
    for key, cursor in value["erc8004"].items():
        if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key)
                or not isinstance(cursor, dict)
                or set(cursor) != {"next_block", "window"}
                or type(cursor["next_block"]) is not int
                or not 0 <= cursor["next_block"] <= 2**60
                or type(cursor["window"]) is not int
                or not 1 <= cursor["window"] <= 4096):
            raise CheckpointError("Invalid ERC-8004 checkpoint cursor")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) +
               "\n").encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise CheckpointError("Oversized checkpoint")
    return encoded


def github_api(method: str, path: str, payload=None):
    """Use a repository-scoped Actions token and GitHub's contents API."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not token or "\r" in token or "\n" in token
            or method not in ("GET", "POST", "PUT")
            or not path.startswith("/") or " " in path):
        raise CheckpointError("GitHub checkpoint configuration missing")
    url = "https://api.github.com/repos/" + repo + path
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SearchA2A-Crawler-Checkpoint/1.0",
    }
    raw = None
    if payload is not None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=raw, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(MAX_STATE_BYTES * 3 + 1)
            if len(data) > MAX_STATE_BYTES * 3:
                raise CheckpointError("Oversized GitHub checkpoint response")
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        # GitHub error bodies may include caller inputs; never display them.
        raise CheckpointError(f"GitHub checkpoint HTTP {exc.code}") from None
    except (ValueError, urllib.error.URLError, OSError) as exc:
        raise CheckpointError("GitHub checkpoint request failed") from exc


def remote_checkpoint():
    """Return the current checkpoint document and GitHub blob SHA, if any."""
    info = github_api("GET", "/contents/" + STATE_PATH + "?ref=" + STATE_BRANCH)
    if info is None:
        return {"version": 1, "erc8004": {}}, None
    if not isinstance(info, dict) or info.get("encoding") != "base64":
        raise CheckpointError("Unexpected checkpoint response")
    sha = info.get("sha")
    blob = info.get("content")
    if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or not isinstance(blob, str)):
        raise CheckpointError("Malformed GitHub checkpoint blob")
    try:
        decoded = base64.b64decode(blob, validate=False)
        if len(decoded) > MAX_STATE_BYTES:
            raise CheckpointError("Oversized checkpoint blob")
        value = json.loads(decoded)
    except (ValueError, UnicodeError) as exc:
        raise CheckpointError("Invalid checkpoint JSON") from exc
    validate_state(value)
    return value, sha


def ensure_state_branch():
    """Create the data branch once, using the checked-out main commit."""
    ref = github_api("GET", "/git/ref/heads/" + STATE_BRANCH)
    if ref is not None:
        return
    main_ref = github_api("GET", "/git/ref/heads/main")
    sha = (main_ref.get("object", {}).get("sha")
           if isinstance(main_ref, dict) else None)
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CheckpointError("Cannot initialize checkpoint branch")
    github_api("POST", "/git/refs", {
        "ref": "refs/heads/" + STATE_BRANCH,
        "sha": sha,
    })


def save_state(staged):
    """Compare and commit monotonic cursors, never overwriting newer progress."""
    validate_state(staged)
    ensure_state_branch()
    current, sha = remote_checkpoint()
    combined = {"version": 1, "erc8004": dict(current["erc8004"])}
    for key, progress in staged["erc8004"].items():
        existing = combined["erc8004"].get(key)
        if existing and progress["next_block"] < existing["next_block"]:
            raise CheckpointError("Refusing stale cursor overwrite")
        combined["erc8004"][key] = progress
    raw = validate_state(combined)
    if combined == current:
        return
    body = {
        "branch": STATE_BRANCH,
        "message": "chore(crawler): advance confirmed ERC-8004 checkpoint [skip ci]",
        "content": base64.b64encode(raw).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    github_api("PUT", "/contents/" + STATE_PATH, body)


def main():
    parser = argparse.ArgumentParser(description="Read/write durable ERC-8004 checkpoints")
    parser.add_argument("action", choices=("load", "save"))
    parser.add_argument("path")
    args = parser.parse_args()
    path = Path(args.path)
    if args.action == "load":
        state, _ = remote_checkpoint()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(validate_state(state))
    else:
        try:
            staged = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SystemExit("Checkpoint file unavailable") from exc
        save_state(staged)
    print("ERC-8004 checkpoint " + ("loaded" if args.action == "load" else "saved"))


if __name__ == "__main__":
    main()
