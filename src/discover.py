#!/usr/bin/env python3
"""Bounded, read-only A2A Agent Card and registry discovery for GitHub Actions."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
from verify_agent import verify_publisher
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MAX_DOCUMENT = 524288
MAX_CANDIDATES = 210
MAX_RESULTS = 150
MAX_GITHUB_CANDIDATES = 50
# Paginated registries are explicit, trusted configuration, never crawler-discovered links.
MAX_REGISTRY_PAGES = 5
REGISTRY_PAGE_SIZE = 30
MAX_REGISTRY_CARD_URLS = 150
USER_AGENT = "SearchA2A/0.1 (+https://github.com/HereLiesAz/searcha2a)"


class DiscoveryError(Exception):
    """An external source is unavailable or unsafe to fetch."""


def require_public_https(url: str) -> str:
    """Refuse credentials, redirects to private networks, and untrusted schemes."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DiscoveryError("Malformed URL") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or port not in (None, 443) or len(url) > 2048):
        raise DiscoveryError("Non-public HTTPS URL")
    host = parsed.hostname.rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise DiscoveryError("Local hostname")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DiscoveryError("DNS resolution failed") from exc
    if not addresses:
        raise DiscoveryError("Host has no resolved addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise DiscoveryError("Host resolves to non-public IP")
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable redirect following for untrusted discovery documents."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))


def fetch_json(url: str, token: str = ""):
    require_public_https(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    try:
        with OPENER.open(urllib.request.Request(url, headers=headers), timeout=7) as response:
            if response.status != 200:
                raise DiscoveryError(f"HTTP {response.status}")
            data = response.read(MAX_DOCUMENT + 1)
            if len(data) > MAX_DOCUMENT:
                raise DiscoveryError("Oversized JSON")
            return json.loads(data)
    except (ValueError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError(f"Could not read JSON: {type(exc).__name__}") from exc


def clean_text(value, max_length: int = 500) -> str:
    return value.strip()[:max_length] if isinstance(value, str) else ""


def normalize_card(card, source_url: str, source: str):
    if not isinstance(card, dict):
        return None
    name = clean_text(card.get("name"), 160)
    skills = card.get("skills")
    if not name or not isinstance(skills, list) or not skills:
        return None
    normalized_skills = []
    for skill in skills[:40]:
        if not isinstance(skill, dict):
            continue
        title = clean_text(skill.get("name") or skill.get("id"), 160)
        if title:
            normalized_skills.append({
                "name": title,
                "description": clean_text(skill.get("description")),
                "tags": [clean_text(tag, 70) for tag in skill.get("tags", [])[:20] if isinstance(tag, str)] if isinstance(skill.get("tags", []), list) else [],
            })
    if not normalized_skills:
        return None
    interfaces = card.get("supportedInterfaces")
    endpoint = clean_text(card.get("url"), 1000)
    protocols = []
    if isinstance(interfaces, list):
        for interface in interfaces[:8]:
            if isinstance(interface, dict):
                if not endpoint:
                    endpoint = clean_text(interface.get("url"), 1000)
                protocols.append(clean_text(interface.get("protocolBinding"), 50))
    # Endpoints are data, not validated or invoked. Only public HTTPS endpoints
    # are advertised as resolvable.
    if not endpoint.startswith("https://"):
        endpoint = ""
    capabilities = card.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    security = card.get("securitySchemes")
    security = sorted(security.keys())[:15] if isinstance(security, dict) else []
    stable = endpoint or source_url
    identity = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]
    return {
        "id": identity,
        "name": name,
        "description": clean_text(card.get("description"), 1200),
        "endpoint": endpoint,
        "source_url": source_url,
        "source": source,
        "skills": normalized_skills,
        "protocols": [p for p in protocols if p],
        "capabilities": {k: bool(capabilities.get(k)) for k in ("streaming", "pushNotifications") if isinstance(capabilities.get(k), bool)},
        "security_schemes": security,
        "version": clean_text(card.get("version"), 80),
        "verification": "card_read_only",  # No L2 protocol probe; don't claim the agent works.
    }


def extract_registry_items(document):
    if isinstance(document, list):
        return document[:MAX_CANDIDATES]
    if not isinstance(document, dict):
        return []
    for key in ("agents", "items", "results", "entries"):
        if isinstance(document.get(key), list):
            return document[key][:MAX_CANDIDATES]
    if isinstance(document.get("data"), list):
        return document["data"][:MAX_CANDIDATES]
    if isinstance(document.get("data"), dict):
        return extract_registry_items(document["data"])
    return []


def candidate_sources(config):
    candidates = []
    for url in config.get("card_urls", []):
        if isinstance(url, str):
            candidates.append((url, "seed"))
    return candidates[:MAX_CANDIDATES]


def github_sources(query: str, token: str):
    if not token:
        return [], ["GitHub search skipped: GITHUB_TOKEN unavailable"]
    results = []
    errors = []
    for filename in ("agent-card.json", "agent.json"):
        text_query = f"{query} filename:{filename}" if query else f"filename:{filename} a2a"
        url = "https://api.github.com/search/code?" + urllib.parse.urlencode({"q": text_query, "per_page": 25})
        try:
            payload = fetch_json(url, token)
            for item in payload.get("items", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict):
                    continue
                raw = item.get("download_url")
                repo = item.get("repository") or {}
                full_name = repo.get("full_name", "") if isinstance(repo, dict) else ""
                path = item.get("path", "")
                branch = repo.get("default_branch", "") if isinstance(repo, dict) else ""
                if not raw and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name) and branch and isinstance(path, str) and path:
                    raw = "https://raw.githubusercontent.com/" + full_name + "/" + urllib.parse.quote(branch, safe="") + "/" + urllib.parse.quote(path, safe="/")
                if isinstance(raw, str):
                    results.append((raw, "github"))
        except DiscoveryError as exc:
            errors.append(f"GitHub {filename}: {str(exc)}")
    if not results:
        # GitHub code search isn't available for every installation token.
        # A bounded repository-search fallback still finds public cards in
        # repositories that advertise A2A functionality.
        repo_query = f"{query} a2a agent" if query else "a2a agent"
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({
            "q": repo_query, "per_page": 12, "sort": "best-match"})
        try:
            payload = fetch_json(url, token)
            for repo in payload.get("items", []) if isinstance(payload, dict) else []:
                full_name = repo.get("full_name", "") if isinstance(repo, dict) else ""
                branch = repo.get("default_branch", "") if isinstance(repo, dict) else ""
                if not (re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name) and branch):
                    continue
                base = "https://raw.githubusercontent.com/" + full_name + "/" + urllib.parse.quote(branch, safe="")
                for path in (".well-known/agent-card.json", ".well-known/agent.json", "agent-card.json"):
                    results.append((base + "/" + path, "github"))
        except DiscoveryError as exc:
            errors.append(f"GitHub repository search: {str(exc)}")
    return results, errors


def rank(record, query: str) -> int:
    if not query:
        return 1
    words = re.findall(r"[\w-]+", query.lower())[:12]
    name = record["name"].lower()
    description = record["description"].lower()
    skill_text = " ".join(s["name"] + " " + s["description"] + " " + " ".join(s["tags"]) for s in record["skills"]).lower()
    return sum(8 * (w in name) + 4 * (w in skill_text) + 1 * (w in description) for w in words)


def discover(query: str, config: dict, token: str = "", fetch=fetch_json, github=github_sources):
    results = {}
    warnings = []
    candidates = candidate_sources(config)
    code_urls, search_warnings = github(query, token)
    warnings.extend(search_warnings)
    # Registry URLs are explicit curated seeds, not arbitrary user-provided URLs.
    for url in config.get("registry_urls", [])[:10]:
        try:
            document = fetch(url)
            for item in extract_registry_items(document):
                if not isinstance(item, dict):
                    continue
                embedded = item.get("card") or item.get("agentCard") or item
                normalized = normalize_card(embedded, url, "registry")
                if normalized:
                    results.setdefault(normalized["id"], normalized)
                else:
                    card_url = item.get("card_url") or item.get("cardUrl")
                    if isinstance(card_url, str):
                        candidates.append((card_url, "registry"))
        except DiscoveryError:
            warnings.append(f"Registry unavailable: {urllib.parse.urlsplit(url).hostname}")
    # Public A2A registries expose Agent Card URLs through a paginated API.
    # Only request URLs derived from configured HTTPS registry origins, never
    # untrusted pagination links or an agent endpoint. Every card is fetched
    # and normalized through the same safety gate as the existing sources.
    for url in config.get("paginated_registry_urls", [])[:2]:
        if not isinstance(url, str):
            continue
        seen = 0
        for page in range(1 if query else MAX_REGISTRY_PAGES):
            if seen >= MAX_REGISTRY_CARD_URLS:
                break
            params = {"limit": REGISTRY_PAGE_SIZE, "offset": page * REGISTRY_PAGE_SIZE}
            if query:
                params["search"] = query
            separator = "&" if urllib.parse.urlsplit(url).query else "?"
            page_url = url + separator + urllib.parse.urlencode(params)
            try:
                records = extract_registry_items(fetch(page_url))
            except (DiscoveryError, ValueError, TypeError):
                warnings.append(f"Registry unavailable: {urllib.parse.urlsplit(url).hostname}")
                break
            for record in records[:REGISTRY_PAGE_SIZE]:
                if not isinstance(record, dict):
                    continue
                card_url = record.get("wellKnownURI")
                if isinstance(card_url, str) and card_url.startswith("https://"):
                    candidates.append((card_url, "registry"))
                    seen += 1
                    if seen >= MAX_REGISTRY_CARD_URLS:
                        break
            if len(records) < REGISTRY_PAGE_SIZE:
                break
    # Reserve capacity for GitHub even when a large registry fills many pages.
    # Deduplicate by URL, not (URL, discovery-origin), before applying budgets.
    def distinct_url(items):
        seen = set()
        for url, origin in items:
            if url not in seen:
                seen.add(url)
                yield url, origin

    github_cards = list(distinct_url(code_urls))[:MAX_GITHUB_CANDIDATES]
    reserved = min(len(github_cards), MAX_GITHUB_CANDIDATES)
    unique = list(distinct_url(candidates))[:MAX_CANDIDATES - reserved]
    existing_urls = {url for url, _ in unique}
    unique.extend((url, origin) for url, origin in github_cards if url not in existing_urls)
    unique = unique[:MAX_CANDIDATES]
    # Cards are read only. Verify a publisher's explicit opt-in proof independently
    # on the same HTTPS origin; no agent task endpoint is contacted.
    def pull(pair):
        url, origin = pair
        try:
            card = fetch(url)
            record = normalize_card(card, url, origin)
            if record:
                evidence = verify_publisher(record, card, fetch=fetch)
                if evidence:
                    record["publisher_verification"] = evidence
            return record
        except (DiscoveryError, ValueError, TypeError):
            return None
    with ThreadPoolExecutor(max_workers=10) as pool:
        for record in pool.map(pull, unique):
            if record:
                results.setdefault(record["id"], record)
    ordered = sorted(results.values(), key=lambda r: (-rank(r, query), r["name"].lower()))
    if query:
        ordered = [r for r in ordered if rank(r, query) > 0]
    return {
        "query": query,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": min(len(ordered), MAX_RESULTS),
        "results": ordered[:MAX_RESULTS],
        "warnings": warnings[:10],
        "verified": "Agent Card metadata only; endpoints are not actively probed",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if len(args.query) > 120:
        parser.error("query exceeds 120 characters")
    config = json.loads((ROOT / "sources.json").read_text())
    data = discover(args.query.strip(), config, token=os.environ.get("GITHUB_TOKEN", ""))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"Found {data['count']} Agent Cards, {len(data['warnings'])} warnings")


if __name__ == "__main__":
    main()
