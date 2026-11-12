#!/usr/bin/env python3
"""Merge a scheduled crawl into SearchA2A's persistent registry; never run on queries."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def ingest(catalog: dict, base: str, secret: str, *, attempts: int = 18,
           opener=urllib.request.urlopen, sleep=time.sleep) -> dict:
    if not base.startswith("https://") or not secret:
        raise ValueError("Registry HTTPS URL and sync secret are required")
    if catalog.get("query") != "" or not isinstance(catalog.get("results"), list):
        raise ValueError("Expected scheduled crawler catalog, not a per-query result")
    if len(catalog["results"]) > 70:
        raise ValueError("Crawler batch exceeds 70 cards")
    payload = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
    if len(payload) > 1000000:
        raise ValueError("Crawl batch exceeds registry size limit")
    endpoint = base.rstrip("/") + "/internal/registry/ingest"
    for attempt in range(attempts):
        request = urllib.request.Request(endpoint, data=payload, method="POST", headers={
            "Authorization": "Bearer " + secret,
            "Content-Type": "application/json",
            "User-Agent": "SearchA2A-Crawler",
        })
        try:
            with opener(request, timeout=15) as response:
                if response.status != 200:
                    raise RuntimeError(f"Registry HTTP {response.status}")
                result = json.load(response)
                if result.get("status") != "stored":
                    raise RuntimeError("Registry did not confirm ingestion")
                return result
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 413):
                raise RuntimeError(f"Registry rejected crawl (HTTP {exc.code})") from exc
            if attempt == attempts - 1:
                raise RuntimeError(f"Registry unavailable after {attempts} attempts") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts - 1:
                raise RuntimeError(f"Registry unavailable after {attempts} attempts") from exc
        sleep(4)
    raise RuntimeError("Registry sync failed")


def filter_export_catalog(catalog: dict, ingest_response: dict) -> dict:
    """Fail closed unless the registry confirms which IDs must stay unpublished."""
    suppressed = ingest_response.get("suppressed_ids")
    if (not isinstance(suppressed, list) or
            any(not isinstance(item, str) or
                len(item) != 20 or
                any(ch not in "0123456789abcdef" for ch in item)
                for item in suppressed)):
        raise ValueError("Registry suppression manifest unavailable or invalid")
    excluded = set(suppressed)
    kept = [card for card in catalog["results"] if card["id"] not in excluded]
    return {**catalog, "results": kept, "count": len(kept)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.catalog.read_text(encoding="utf-8"))
    result = ingest(data, os.getenv("REGISTRY_API_URL", ""), os.getenv("REGISTRY_SYNC_SECRET", ""))
    publishable = filter_export_catalog(data, result)
    args.catalog.write_text(json.dumps(publishable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Registry now contains {result['total']} agents; ingested {result['ingested']} cards; "
          f"published {publishable['count']} after curation")


if __name__ == "__main__":
    main()
