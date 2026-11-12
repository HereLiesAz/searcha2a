#!/usr/bin/env python3
"""Export read-only Azphalt Store discovery listings from SearchA2A card metadata.

These are external agents, not signed .azp packages, installable model weights, or
first-party Azphalt extensions. The store must render them in a separate directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_listings(catalog: dict) -> dict:
    """Adapt the bounded public card index into a non-installable store directory."""
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
            "verification": "card_read_only",
            # Discovery hint only. The promoter re-fetches the card and proof.
            **({"publisher_verification": card["publisher_verification"]}
               if isinstance(card.get("publisher_verification"), dict) and
                  card["publisher_verification"].get("method") == "https-origin-opt-in"
               else {}),
            "installable": False,
            "source": "searcha2a",
        })
    return {
        "schema": 1,
        "source": "searcha2a",
        "generated_at": catalog.get("generated_at"),
        "count": len(entries),
        "listings": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_listings(json.loads(args.catalog.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {result['count']} external A2A discovery listings")


if __name__ == "__main__":
    main()
