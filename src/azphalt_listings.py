#!/usr/bin/env python3
"""Export read-only Azphalt Store discovery listings from SearchA2A card metadata.

These are external agents, not signed .azp packages, installable model weights, or
first-party Azphalt extensions. The store must render them in a separate directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

LEVEL_NAMES = ["card_read", "origin_consistent", "endpoint_reachable", "domain_control_proven"]
AUTH_TYPES = {"apiKey", "basic", "bearer", "mtls", "oauth", "openid", "other", "x402"}
KEYLESS = {"declared", "no", "unknown"}  # never "observed"
PAYMENT = {"declared", "none_declared", "unknown"}


def _parse_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        return None


def _access(card: dict, evidence):
    """Declared key/payment facts from the card; fresh negative evidence may only move toward 'no'."""
    access = card.get("access")
    if not (isinstance(access, dict) and access.get("keyless") in KEYLESS
            and access.get("payment_required") in PAYMENT
            and isinstance(access.get("auth_types"), list)
            and all(t in AUTH_TYPES for t in access["auth_types"])):
        return None
    keyless = access["keyless"]
    observed = evidence.get("access") if isinstance(evidence, dict) else None
    if (isinstance(observed, dict) and observed.get("keyless") == "no"
            and observed.get("observed_challenge") in ("401", "402", "403")):
        keyless = "no"
    return {"auth_types": list(access["auth_types"]), "keyless": keyless,
            "payment_required": access["payment_required"]}


def _backing(card: dict):
    backing = card.get("backing")
    if not isinstance(backing, dict):
        return None
    declared = backing.get("declared_provider")
    hosting = backing.get("hosting")
    model = backing.get("model_provider")
    out_declared = None
    if isinstance(declared, dict) and isinstance(declared.get("organization"), str):
        host = ""
        if isinstance(declared.get("url"), str) and declared["url"].startswith("https://"):
            host = (urlsplit(declared["url"]).hostname or "")[:253]
        out_declared = {"organization": declared["organization"][:160], "url_host": host}
    platform = hosting.get("platform") if isinstance(hosting, dict) else None
    return {"declared_provider": out_declared,
            "hosting": platform[:40] if isinstance(platform, str) else None,
            "model_provider": model["name"][:80] if isinstance(model, dict)
            and isinstance(model.get("name"), str) else None}


def _verification_detail(evidence, now: datetime):
    """Only well-formed, unexpired evidence is published; anything else is omitted (read as level 0)."""
    detail = evidence.get("verification_detail") if isinstance(evidence, dict) else None
    if not isinstance(detail, dict):
        return None, None
    level = detail.get("level")
    if (isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 3
            or detail.get("level_name") != LEVEL_NAMES[level]
            or not isinstance(detail.get("reviewed"), bool)):
        return None, None
    expires = _parse_time(detail.get("expires_at"))
    if expires is None or expires < now:
        return None, None
    out = {"level": level, "level_name": detail["level_name"], "reviewed": detail["reviewed"],
           "checked_at": detail.get("checked_at") if isinstance(detail.get("checked_at"), str) else None,
           "expires_at": detail["expires_at"]}
    computed = detail.get("computed_at")
    return out, computed if isinstance(computed, str) and len(computed) <= 40 else None


def _additive(card: dict, evidence, clock: datetime) -> dict:
    out = {}
    access = _access(card, evidence)
    if access:
        out["access"] = access
    backing = _backing(card)
    if backing:
        out["backing"] = backing
    detail, computed = _verification_detail(evidence, clock)
    if detail:
        out["verification_detail"] = detail
        if computed:
            out["registry_checked_at"] = computed
    return out


def build_listings(catalog: dict, evidence=None, now: str | None = None) -> dict:
    """Adapt the bounded public card index into a non-installable store directory.

    Schema stays 1 and `verification` stays "card_read_only" because other apps pin both.
    Additive data (schema_revision 1.1): `access`, `backing`, and, when a fresh evidence export
    is supplied, `verification_detail`. Missing evidence is simply absent, which readers treat
    as level 0: evidence can only be lost, never invented.
    """
    clock = (_parse_time(now) if now else None) or datetime.now(timezone.utc)
    evidence = evidence if isinstance(evidence, dict) else {}
    entries = []
    seen: set[str] = set()
    for card in catalog.get("results", []):
        if not isinstance(card, dict):
            continue
        card_id = card.get("id")
        name = card.get("name")
        source = card.get("source_url")
        if not (isinstance(card_id, str) and len(card_id) == 20
                and all(c in "0123456789abcdef" for c in card_id)
                and isinstance(name, str) and name.strip()
                and isinstance(source, str) and source.startswith("https://")):
            continue
        if card_id in seen:
            continue
        seen.add(card_id)
        skills = card.get("skills", [])
        skills = skills if isinstance(skills, list) else []
        entries.append({
            "id": f"external.a2a.{card_id}",
            "kind": "a2a-agent",
            "name": name[:160],
            "description": card.get("description", "")[:1200] if isinstance(card.get("description"), str) else "",
            "endpoint": card.get("endpoint", "") if isinstance(card.get("endpoint"), str) else "",
            "source_url": source,
            "discovery_url": f"https://searcha2a.azphalt.store/api/v1/agents/{card_id}.json",
            "skills": [{"name": skill.get("name", "")[:160],
                        "description": skill.get("description", "")[:500],
                        "tags": skill.get("tags", [])[:20]}
                       for skill in skills[:40]
                       if isinstance(skill, dict) and isinstance(skill.get("name"), str)
                       and isinstance(skill.get("description", ""), str)
                       and isinstance(skill.get("tags", []), list)],
            "protocols": card.get("protocols", []) if isinstance(card.get("protocols"), list) else [],
            "security_schemes": card.get("security_schemes", []) if isinstance(card.get("security_schemes"), list) else [],
            "capabilities": card.get("capabilities", {}) if isinstance(card.get("capabilities"), dict) else {},
            # Same-origin opt-in is only a discovery hint, not ANS or signing proof.
            **({"publisher_verification": card["publisher_verification"]}
               if isinstance(card.get("publisher_verification"), dict) and
                  card["publisher_verification"].get("method") == "https-origin-opt-in"
               else {}),
            "verification": "card_read_only",
            **({"ans_claim": {"name": card["ans_name"], "agent_host": card["ans_host"],
                              "verification": "unverified"}}
               if isinstance(card.get("ans_name"), str) and isinstance(card.get("ans_host"), str) else {}),
            "installable": False,
            "source": "searcha2a",
            **_additive(card, evidence.get(card_id), clock),
        })
    return {
        "schema": 1,
        "schema_revision": "1.1",
        "source": "searcha2a",
        "generated_at": catalog.get("generated_at"),
        "count": len(entries),
        "listings": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path,
                        help="Optional evidence export from registry_sync.py; ignored if unreadable")
    args = parser.parse_args()
    evidence = None
    if args.evidence and args.evidence.exists():
        try:
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        except ValueError:
            evidence = None  # fail soft: listings publish without verification_detail
    result = build_listings(json.loads(args.catalog.read_text(encoding="utf-8")), evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {result['count']} external A2A discovery listings")


if __name__ == "__main__":
    main()
