#!/usr/bin/env python3
"""Turn freshly verified SearchA2A Agent Cards into reviewable kind:llm A2A submissions.

Only external, signed-header *source* is produced here. Azphalt's existing
submission verifier and registry-sync sign real .azp packages using the
existing publisher key. This script never holds a signing key or executes an
agent. A missing opt-in proof is never elevated to trust.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from discover import DiscoveryError, MAX_CATALOG_DOCUMENT, fetch_json, normalize_card
from verify_agent import verify_publisher

CATALOG_URL = "https://raw.githubusercontent.com/HereLiesAz/searcha2a/agent-feed/site/api/v1/azphalt-listings.json"
BOT_AUTHOR = "Azphalt automated A2A publisher-verification"
PREFIX = "org.azphalt.a2a."
MAX_LEADS = 150
MAX_NEW = 12
LICENSE = """MIT License

Copyright (c) 2026 Azphalt and the consenting Agent Card publisher

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def _clean(value: str, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]


def _auth(card: dict):
    """Refuse security schemes a generic A2A bearer connection cannot honor."""
    schemes = card.get("securitySchemes") or {}
    if not isinstance(schemes, dict):
        return None
    if not schemes:
        return {"auth": "none"}, []
    if len(schemes) != 1:
        return None
    scheme = next(iter(schemes.values()))
    if not isinstance(scheme, dict):
        return None
    http = scheme.get("httpAuthSecurityScheme") or scheme
    if not isinstance(http, dict) or str(http.get("scheme", "")).lower() != "bearer":
        return None
    return {"auth": "required-bearer", "authInput": "providerKey"}, [
        {"id": "providerKey", "type": "promptString", "password": True,
         "description": "Bearer token for the external A2A service"}
    ]


def _id(card_url: str) -> str:
    return PREFIX + hashlib.sha256(card_url.encode("utf-8")).hexdigest()[:20]


def _manifest(record: dict, original: dict):
    """Construct only metadata with explicit rights, never executable code."""
    auth = _auth(original)
    if auth is None:
        return None
    endpoint_auth, inputs = auth
    origin = urlsplit(record["source_url"])
    return {
        "azphalt": "0.1",
        "id": _id(record["source_url"]),
        "name": _clean(record["name"], 160),
        "version": "1.0.0",
        "kind": "llm",
        "license": "MIT",
        "compat": ">=0.1",
        "description": _clean(record.get("description") or "Remote A2A agent; authorization is separate from package signing.", 1200),
        "author": BOT_AUTHOR,
        "homepage": "https://" + origin.hostname,
        "llm": {
            "tier": "endpoint",
            **({"inputs": inputs} if inputs else {}),
            "setup": {"sandbox": "github-actions", "script": "setup/setup.sh", "fetches": []},
            "endpoint": {
                "protocols": ["a2a-jsonrpc"],
                "baseUrl": record["endpoint"],
                "agentCardUrl": record["source_url"],
                **endpoint_auth,
            },
            "dataHandling": {"prompts": "unknown", "operator": origin.hostname},
            "role": "agent",
        },
    }


# This generated script is fixed, not synthesized from card-supplied shell.
# Card fields are placed in signed JSON. Running it never executes an agent.
SETUP_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
python3 - "$here/agent.json" <<'PY'
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if data.get("protocol") != "a2a-jsonrpc":
    raise SystemExit("Unsupported agent protocol")
card_url = data.get("agentCardUrl")
endpoint = data.get("baseUrl")
if not isinstance(card_url, str) or not isinstance(endpoint, str):
    raise SystemExit("Missing A2A endpoint or Agent Card")
card, api = urlsplit(card_url), urlsplit(endpoint)
if card.scheme != "https" or api.scheme != "https" or (card.hostname or "").lower() != (api.hostname or "").lower():
    raise SystemExit("A2A card and endpoint must share an HTTPS origin")
if card.username or card.password or api.username or api.password or card.port not in (None, 443) or api.port not in (None, 443):
    raise SystemExit("A2A endpoint contains credentials or an alternate port")
if card.query or card.fragment or api.query or api.fragment:
    raise SystemExit("A2A endpoint cannot contain query parameters or fragments")
if data.get("auth") not in ("none", "required-bearer"):
    raise SystemExit("Unsupported A2A authentication type")
print("A2A configuration validated. Host must revalidate Agent Card and request consent.")
PY
"""


def _connection(manifest: dict) -> dict:
    endpoint = manifest["llm"]["endpoint"]
    return {
        "protocol": "a2a-jsonrpc",
        "agentCardUrl": endpoint["agentCardUrl"],
        "baseUrl": endpoint["baseUrl"],
        "auth": endpoint["auth"],
    }


def _write_submission(folder: Path, manifest: dict):
    """Write a fully deterministic script and data; no publisher code is bundled."""
    setup = folder / "setup"
    if setup.is_symlink():
        raise ValueError("Refusing symlink setup directory")
    setup.mkdir(exist_ok=True)
    for file in (folder / "manifest.json", setup / "agent.json", setup / "setup.sh"):
        if file.is_symlink():
            raise ValueError("Refusing symlink output file: " + str(file))
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (setup / "agent.json").write_text(json.dumps(_connection(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (setup / "setup.sh").write_text(SETUP_SCRIPT, encoding="utf-8")


def _verified_manifest(card_url: str, fetch):
    try:
        original = fetch(card_url)
        record = normalize_card(original, card_url, "publisher-revalidated")
        if not record or verify_publisher(record, original, fetch=fetch) is None:
            return None
        return _manifest(record, original)
    except (DiscoveryError, ValueError, TypeError, KeyError, OSError):
        return None


def _existing_manifest(folder: Path):
    if folder.is_symlink():
        raise ValueError("Refusing symlink submission directory: " + str(folder))
    file = folder / "manifest.json"
    if file.is_symlink():
        raise ValueError("Refusing symlink manifest: " + str(file))
    try:
        manifest = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Refusing to modify unrecognized submission: " + str(folder))
    if not (isinstance(manifest, dict) and manifest.get("id") == folder.name and
            manifest.get("author") == BOT_AUTHOR and manifest.get("kind") == "llm" and
            isinstance(manifest.get("llm"), dict) and
            isinstance(manifest["llm"].get("endpoint"), dict) and
            manifest["llm"]["endpoint"].get("protocols") == ["a2a-jsonrpc"]):
        raise ValueError("Refusing to overwrite a non-automated submission: " + str(folder))
    files = {p.name for p in folder.iterdir()}
    if files != {"manifest.json", "LICENSE", "setup"} or (folder / "LICENSE").is_symlink():
        raise ValueError("Refusing unexpected files in automated submission: " + str(folder))
    setup = folder / "setup"
    if setup.is_symlink() or not setup.is_dir() or {p.name for p in setup.iterdir()} != {"agent.json", "setup.sh"}:
        raise ValueError("Refusing unrecognized setup payload: " + str(folder))
    if any((setup / p).is_symlink() for p in ("agent.json", "setup.sh")):
        raise ValueError("Refusing symlink setup payload: " + str(folder))
    if (folder / "LICENSE").read_text(encoding="utf-8") != LICENSE:
        raise ValueError("Automated package license was modified: " + str(folder))
    if (setup / "setup.sh").read_text(encoding="utf-8") != SETUP_SCRIPT:
        raise ValueError("Automated setup script was modified: " + str(folder))
    try:
        signed_data = json.loads((setup / "agent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Automated connection metadata is invalid: " + str(folder))
    if signed_data != _connection(manifest):
        raise ValueError("Automated connection metadata was modified: " + str(folder))
    return manifest


def promote(feed: dict, submissions: Path, *, fetch=fetch_json, max_new=MAX_NEW):
    """Fail closed. Refresh existing entries; add up to max_new opt-in publishers."""
    if not isinstance(feed, dict) or feed.get("schema") != 1 or feed.get("source") != "searcha2a" or not isinstance(feed.get("listings"), list):
        raise ValueError("Unexpected SearchA2A feed")
    if max_new < 0 or max_new > MAX_NEW:
        raise ValueError("Invalid per-run new-listing limit")
    submissions.mkdir(parents=True, exist_ok=True)
    existing = {}
    for folder in sorted(submissions.glob(PREFIX + "*")):
        if folder.is_dir():
            manifest = _existing_manifest(folder)
            existing[manifest["id"]] = (folder, manifest)

    leads = {}
    for item in feed["listings"][:MAX_LEADS]:
        if not isinstance(item, dict) or item.get("kind") != "a2a-agent" or item.get("installable") is not False:
            continue
        if item.get("verification") != "card_read_only":
            continue
        # The SearchA2A crawler already checked opt-in on a freshly fetched card.
        # Revalidate all proofs again before generating anything.
        if not isinstance(item.get("publisher_verification"), dict):
            continue
        url = item.get("source_url")
        if isinstance(url, str) and url.startswith("https://") and len(url) <= 2048:
            leads[_id(url)] = url
    # Recheck already published headers even if the latest bounded crawl missed them.
    for package_id, (_folder, manifest) in existing.items():
        leads[package_id] = manifest["llm"]["endpoint"]["agentCardUrl"]

    def verify(pair):
        package_id, card_url = pair
        m = _verified_manifest(card_url, fetch)
        return package_id, m if m and m["id"] == package_id else None

    checks = dict(ThreadPoolExecutor(max_workers=8).map(verify, sorted(leads.items())))
    created, updated, revoked = [], [], []
    for package_id, (folder, old) in existing.items():
        candidate = checks.get(package_id)
        if candidate is None:
            # Transient crawler outages cannot delete already published packages.
            # Quarantine/removal uses a separate reviewed revocation path.
            revoked.append(package_id)
            continue
        old_version = old["version"]
        candidate["version"] = old_version
        if candidate == old and (folder / "setup" / "agent.json").exists() and (folder / "setup" / "setup.sh").exists():
            continue
        match = re.fullmatch(r"1\.0\.(0|[1-9][0-9]*)", old_version)
        if not match:
            raise ValueError("Invalid automated package version: " + package_id)
        candidate["version"] = "1.0." + str(int(match.group(1)) + 1)
        _write_submission(folder, candidate)
        updated.append(package_id)
    for package_id, candidate in sorted(checks.items()):
        if candidate is None or package_id in existing or len(created) >= max_new:
            continue
        folder = submissions / package_id
        if folder.exists() or folder.is_symlink():
            raise ValueError("Refusing to overwrite a non-automated submission: " + str(folder))
        folder.mkdir()
        _write_submission(folder, candidate)
        (folder / "LICENSE").write_text(LICENSE, encoding="utf-8")
        created.append(package_id)
    return {"created": created, "updated": updated, "revoked": revoked}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--submissions", type=Path, required=True)
    parser.add_argument("--max-new", type=int, default=MAX_NEW)
    args = parser.parse_args()
    report = promote(fetch_json(CATALOG_URL, max_bytes=MAX_CATALOG_DOCUMENT), args.submissions, max_new=args.max_new)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
