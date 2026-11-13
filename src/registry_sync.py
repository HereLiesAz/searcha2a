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


MAX_BATCH_BYTES = 1_000_000
MAX_BATCH_CARDS = 70


def ingest(catalog: dict, base: str, secret: str, *, attempts: int = 18,
           opener=urllib.request.urlopen, sleep=time.sleep) -> dict:
    """Upsert bounded batches, returning the union of every curation suppression.

    The registry validates each batch atomically. Earlier successful batches remain
    stored if a later batch fails; publication fails closed and the next scheduled
    crawl retries idempotent upserts. No interactive query triggers this path.
    """
    if not base.startswith("https://") or not secret:
        raise ValueError("Registry HTTPS URL and sync secret are required")
    if catalog.get("query") != "" or not isinstance(catalog.get("results"), list):
        raise ValueError("Expected scheduled crawler catalog, not a per-query result")
    records = catalog["results"]
    if attempts < 1:
        raise ValueError("At least one ingest attempt is required")
    metadata = {key: value for key, value in catalog.items()
                if key not in ("results", "count")}
    batches: list[list] = []
    current: list = []
    for agent in records:
        proposed = current + [agent]
        payload = json.dumps({**metadata, "results": proposed, "count": len(proposed)},
                             ensure_ascii=False).encode("utf-8")
        if len(proposed) > MAX_BATCH_CARDS or len(payload) > MAX_BATCH_BYTES:
            if not current:
                raise ValueError("One Agent Card exceeds the registry upload size limit")
            batches.append(current)
            current = [agent]
            single = json.dumps({**metadata, "results": current, "count": 1},
                                ensure_ascii=False).encode("utf-8")
            if len(single) > MAX_BATCH_BYTES:
                raise ValueError("One Agent Card exceeds the registry upload size limit")
        else:
            current = proposed
    if current or not batches:
        batches.append(current)

    endpoint = base.rstrip("/") + "/internal/registry/ingest"
    suppressed: set[str] = set()
    ingested = 0
    total = 0
    for batch in batches:
        payload = json.dumps({**metadata, "results": batch, "count": len(batch)},
                             ensure_ascii=False).encode("utf-8")
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
                    if result.get("status") != "stored" or result.get("ingested") != len(batch):
                        raise RuntimeError("Registry did not confirm complete ingestion")
                    manifest = result.get("suppressed_ids")
                    if not isinstance(manifest, list) or any(
                        not isinstance(i, str) or len(i) != 20 or
                        any(ch not in "0123456789abcdef" for ch in i)
                        for i in manifest
                    ):
                        raise ValueError("Registry suppression manifest unavailable or invalid")
                    suppressed.update(manifest)
                    total = result["total"]
                    ingested += len(batch)
                    break
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401, 403, 413):
                    raise RuntimeError(f"Registry rejected crawl (HTTP {exc.code})") from exc
                if attempt == attempts - 1:
                    raise RuntimeError(f"Registry unavailable after {attempts} attempts") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == attempts - 1:
                    raise RuntimeError(f"Registry unavailable after {attempts} attempts") from exc
            sleep(4)
    return {"status": "stored", "ingested": ingested, "total": total,
            "suppressed_ids": sorted(suppressed)}


def fetch_evidence(base: str, token: str, *, opener=urllib.request.urlopen):
    """Read-time verification summaries for publication. Fails soft: None means publish without them."""
    if not base.startswith("https://") or not token:
        return None
    request = urllib.request.Request(
        base.rstrip("/") + "/internal/registry/evidence-export", method="GET",
        headers={"Authorization": "Bearer " + token, "User-Agent": "SearchA2A-Crawler"})
    try:
        with opener(request, timeout=20) as response:
            if response.status != 200:
                return None
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or len(entries) > 5000:
        return None
    return {entry["id"]: entry for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and len(entry["id"]) == 20
            and all(ch in "0123456789abcdef" for ch in entry["id"])}


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
    parser.add_argument("--catalog", type=Path, help="Crawl catalog to ingest")
    parser.add_argument("--evidence-export", type=Path,
                        help="Instead of ingesting, save the registry's verification evidence here "
                             "(best-effort: exits 0 and writes nothing if unavailable)")
    args = parser.parse_args()
    base = os.getenv("REGISTRY_API_URL", "")
    token = os.getenv("REGISTRY_SYNC_SECRET", "")
    if args.evidence_export:
        evidence = fetch_evidence(base, token)
        if evidence is None:
            print("Evidence export unavailable; listings will carry no verification_detail")
        else:
            args.evidence_export.write_text(json.dumps(evidence), encoding="utf-8")
            print(f"Saved verification evidence for {len(evidence)} agents")
        return
    if not args.catalog:
        parser.error("--catalog is required unless --evidence-export is used")
    data = json.loads(args.catalog.read_text(encoding="utf-8"))
    result = ingest(data, base, token)
    publishable = filter_export_catalog(data, result)
    args.catalog.write_text(json.dumps(publishable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Registry now contains {result['total']} agents; ingested {result['ingested']} cards; "
          f"published {publishable['count']} after curation")


if __name__ == "__main__":
    main()
