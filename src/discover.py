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
import re
import socket
from verify_agent import verify_publisher
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MAX_DOCUMENT = 524288
# The aggregate Azphalt listings feed can contain up to 150 bounded Agent Cards.
MAX_CATALOG_DOCUMENT = 16_777_216
MAX_CANDIDATES = 350
MAX_RESULTS = 150
MAX_GITHUB_CANDIDATES = 50
MAX_DOMAIN_SEEDS = 12
MAX_MANIFEST_ITEMS = 150
MAX_FOLLOWUP_CARD_URLS = 100
MAX_RAW_CARD_BYTES = 4096
# Paths are literal; unknown assistant identifiers are never enumerated.
PROBE_PATHS = (
    '/.well-known/agent-card.json', '/.well-known/agent.json',
    '/.well-known/agents.json', '/agents.json', '/agent.json',
    '/a2a/.well-known/agent.json', '/.wellknown/agent-card.json',
)
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


def fetch_json(url: str, token: str = "", *, max_bytes: int = MAX_DOCUMENT):
    """Fetch a bounded JSON document. Larger limits are only for fixed aggregate feeds."""
    if max_bytes < 1 or max_bytes > MAX_CATALOG_DOCUMENT:
        raise DiscoveryError("Invalid document size bound")
    require_public_https(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    try:
        with OPENER.open(urllib.request.Request(url, headers=headers), timeout=7) as response:
            if response.status != 200:
                raise DiscoveryError(f"HTTP {response.status}")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
            if content_type != "application/json" and not (content_type.startswith("application/") and content_type.endswith("+json")):
                raise DiscoveryError("Non-JSON response Content-Type")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
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
    if not name or not isinstance(skills, list):
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
    interfaces = card.get("supportedInterfaces")
    # Tolerate public A2A cards with no declared skills only when an A2A
    # interface exists; never turn ordinary named JSON into an agent.
    if not normalized_skills and not (isinstance(interfaces, list) and any(
            isinstance(interface, dict) and isinstance(interface.get("url"), str)
            and interface["url"].startswith("https://") for interface in interfaces)):
        return None
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
    # Preserve complete publisher-defined names; truncation would silently change identity.
    raw_name = card.get("ansName") or card.get("ans_name")
    raw_host = card.get("agentHost") or card.get("ansHost") or card.get("ans_host")
    ans_name = raw_name if isinstance(raw_name, str) and len(raw_name) <= 259 else ""
    ans_host = raw_host if isinstance(raw_host, str) and len(raw_host) <= 251 else ""
    raw = json.dumps(card, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
        "schema_status": "declared_skills" if normalized_skills else "partial_agent_card",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        **({"raw_card": card} if len(raw) <= MAX_RAW_CARD_BYTES else {}),
        **({"ans_name": ans_name, "ans_host": ans_host} if ans_name and ans_host else {}),
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


def public_card_link(value: str, base_url: str) -> str:
    """Resolve only explicit JSON Agent Card pointers; never call execution URLs."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return ""
    try:
        url = urllib.parse.urljoin(base_url, value.strip())
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
        if parsed.port not in (None, 443) or parsed.fragment:
            return ""
        if not parsed.path.lower().endswith(".json"):
            return ""
        return url
    except ValueError:
        return ""


def card_references(item, source_url: str):
    """Only structural card URL fields are traversed, never skill text or task endpoints."""
    if not isinstance(item, dict):
        return []
    urls = []
    for key in ("wellKnownURI", "card_url", "cardUrl", "agentCardUrl",
                "agentCardURI", "agentURI", "agentUri", "agent_card_url",
                "agent_card_uri", "card"):
        value = item.get(key)
        if isinstance(value, str):
            link = public_card_link(value, source_url)
            if link:
                urls.append(link)
    # Some manifests use {protocol:"a2a", url:".../card.json"}.
    protocol = str(item.get("protocol", "")).lower()
    if protocol == "a2a" or protocol == "agent2agent":
        link = public_card_link(item.get("url"), source_url)
        if link:
            urls.append(link)
    return list(dict.fromkeys(urls))


def unpack_cards(document, source_url: str, source: str):
    """Read a single card or bounded multi-agent manifest; collect one-hop card links."""
    direct = normalize_card(document, source_url, source)
    if direct:
        return [direct], []
    cards, links = [], []
    for item in extract_registry_items(document)[:MAX_MANIFEST_ITEMS]:
        if not isinstance(item, dict):
            continue
        embedded = item.get("card") or item.get("agentCard") or item
        normalized = normalize_card(embedded, source_url, source)
        if normalized:
            cards.append(normalized)
        links.extend(card_references(item, source_url))
    return cards, list(dict.fromkeys(links))


def domain_sources(config):
    """Probe only explicitly configured domains; no wildcard, port or tenant guessing."""
    sources = []
    assistants = config.get("assistant_ids", {})
    if not isinstance(assistants, dict):
        assistants = {}
    for base in config.get("domain_urls", [])[:MAX_DOMAIN_SEEDS]:
        if not isinstance(base, str):
            continue
        try:
            parsed = urllib.parse.urlsplit(base)
            if (parsed.scheme != "https" or not parsed.hostname or
                    parsed.username or parsed.password or parsed.port not in (None, 443) or
                    parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                continue
        except ValueError:
            continue
        origin = base.rstrip("/")
        sources.extend((origin + path, "domain-probe") for path in PROBE_PATHS)
        ids = assistants.get(origin, [])
        if isinstance(ids, list):
            for assistant_id in ids[:10]:
                if isinstance(assistant_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", assistant_id):
                    sources.append((origin + "/.well-known/agent-card.json?assistant_id=" +
                                    urllib.parse.quote(assistant_id), "assistant-probe"))
    return sources


def candidate_sources(config):
    candidates = []
    for url in config.get("card_urls", []):
        if isinstance(url, str):
            candidates.append((url, "seed"))
    candidates.extend(domain_sources(config))
    return candidates[:MAX_CANDIDATES]


def github_sources(query: str, token: str):
    if not token:
        return [], ["GitHub search skipped: GITHUB_TOKEN unavailable"]
    results = []
    errors = []
    for filename in ("agent-card.json", "agent.json", "agents.json"):
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
                for path in (".well-known/agent-card.json", ".well-known/agent.json",
                             ".well-known/agents.json", "agents.json", "agent-card.json"):
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
            embedded, links = unpack_cards(document, url, "registry")
            for record in embedded:
                results.setdefault(record["id"], record)
            candidates.extend((link, "registry") for link in links)
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
                for card_url in card_references(record, page_url):
                    if seen >= MAX_REGISTRY_CARD_URLS:
                        break
                    candidates.append((card_url, "registry"))
                    seen += 1
            if len(records) < REGISTRY_PAGE_SIZE:
                break
    # Preserve bounded GitHub discovery even when explicit registries are crowded.
    # Dedupe by the actual card URL, independently of the originating registry.
    def distinct_url(items):
        seen = set()
        for url, origin in items:
            if url not in seen:
                seen.add(url)
                yield url, origin

    github_cards = list(distinct_url(code_urls))[:MAX_GITHUB_CANDIDATES]
    reserved = len(github_cards)
    unique = list(distinct_url(candidates))[:MAX_CANDIDATES - reserved]
    existing_urls = {url for url, _ in unique}
    unique.extend((url, origin) for url, origin in github_cards if url not in existing_urls)
    unique = unique[:MAX_CANDIDATES]
    # Fetch metadata only. A publisher opt-in is checked against a same-origin
    # HTTPS proof, never mistaken for cryptographic ANS identity.
    def pull(pair):
        url, origin = pair
        try:
            document = fetch(url)
            cards, links = unpack_cards(document, url, origin)
            if len(cards) == 1 and isinstance(document, dict) and cards[0]["source_url"] == url:
                evidence = verify_publisher(cards[0], document, fetch=fetch)
                if evidence:
                    cards[0]["publisher_verification"] = evidence
            return cards, links
        except (DiscoveryError, ValueError, TypeError, OverflowError, RecursionError):
            return [], []

    discovered = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for cards, links in pool.map(pull, unique):
            for record in cards:
                results.setdefault(record["id"], record)
            discovered.extend(links)

    # At most one further GET hop. Do not spider arbitrary links, invoke A2A
    # tasks/MCP tools, or let manifests override a publisher's own card.
    visited = {url for url, _ in unique}
    followups = []
    for url in discovered:
        if url not in visited:
            visited.add(url)
            followups.append((url, "manifest"))
            if len(followups) >= MAX_FOLLOWUP_CARD_URLS:
                break
    with ThreadPoolExecutor(max_workers=10) as pool:
        for cards, _links in pool.map(pull, followups):
            for record in cards:
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
