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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.catalog.read_text(encoding="utf-8"))
    result = ingest(data, os.getenv("REGISTRY_API_URL", ""), os.getenv("REGISTRY_SYNC_SECRET", ""))
    print(f"Registry now contains {result['total']} agents; ingested {result['ingested']} cards")


if __name__ == "__main__":
    main()
