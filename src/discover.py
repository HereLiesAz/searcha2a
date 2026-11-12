#!/usr/bin/env python3
"""Bounded, read-only A2A Agent Card and registry discovery for GitHub Actions."""
from __future__ import annotations

import argparse
import base64
import binascii
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
import crawl_retry
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MAX_DOCUMENT = 524288
# Aggregate feeds retain a byte ceiling; the scheduled result count is not capped.
MAX_CATALOG_DOCUMENT = 16_777_216
MAX_PUBLIC_REGISTRY_JSON = 8_388_608
MAX_CANDIDATES = 350
MAX_GITHUB_CANDIDATES = 50
MAX_DOMAIN_SEEDS = 12
MAX_MANIFEST_ITEMS = 150
MAX_FOLLOWUP_CARD_URLS = 100
MAX_RAW_CARD_BYTES = 4096
MAX_PRIVATE_CATALOGS = 5
MAX_INLINE_NESTED_CATALOGS = 8
MAX_WEBFINGER_ACCOUNTS = 8
MAX_DNS_DOMAINS = 12
MAX_DNS_TXT_ANSWERS = 8
MAX_ERC8004_RPC_SOURCES = 4
MAX_ERC8004_BLOCK_WINDOW = 512
MAX_ERC8004_LOGS = 120
MAX_ERC8004_CHECKPOINT_WINDOW = 4096
MAX_ERC8004_WINDOWS_PER_RUN = 16
MAX_ERC8004_ATTEMPTS_PER_RUN = 24
MAX_ERC8004_CHECKPOINT_URLS_PER_SOURCE = 40
MAX_OCI_SOURCES = 4
MAX_OCI_LAYERS = 6
MAX_OCI_DOCUMENT = 524288
MAX_MCP_SOURCES = 4
MAX_MCP_PAGES = 3
MAX_MCP_RESOURCES_PER_PAGE = 50
MAX_MCP_RESOURCE_READS = 10
MAX_MCP_DOCUMENT = 262144
MAX_PUBLIC_DISCOVERY_APIS = 2
MAX_PUBLIC_DISCOVERY_TOPICS_PER_RUN = 3
MAX_PUBLIC_DISCOVERY_ROWS = 25
MAX_PUBLIC_DISCOVERY_BYTES = 262144
PUBLIC_DISCOVERY_TOPICS = (
    'research', 'coding', 'legal', 'automation', 'memory',
    'identity', 'commerce', 'monitoring', 'security', 'translation',
    'finance', 'media', 'marketing', 'trading', 'travel',
)
PUBLIC_DISCOVERY_ENDPOINTS = frozenset({'https://allagents.app/discover'})
MCP_PROTOCOL_VERSION = "2026-07-28"
REGISTERED_TOPIC = "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a"
URI_UPDATED_TOPIC = "0x3a2c7fffc2cba7582c690e3b82c453ea02a308326a98a3ad7576c606336409fb"
A2A_CATALOG_TYPES = {'application/a2a-agent-card+json', 'application/a2a+json'}
# Paths are literal; unknown assistant identifiers are never enumerated.
PROBE_PATHS = (
    '/.well-known/agent-card.json', '/.well-known/agent.json',
    '/.well-known/agents.json', '/agents.json', '/agent.json',
    '/a2a/.well-known/agent.json', '/.wellknown/agent-card.json',
    '/agent-card.json', '/.well-known/ai-catalog.json',
    '/.well-known/ard.json', '/.well-known/agent-registration.json',
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


def fetch_json(url: str, token: str = "", *, max_bytes: int = MAX_DOCUMENT, private_auth=None):
    """Fetch a bounded JSON document. Larger limits are only for fixed aggregate feeds."""
    if max_bytes < 1 or max_bytes > MAX_CATALOG_DOCUMENT:
        raise DiscoveryError("Invalid document size bound")
    require_public_https(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if private_auth is not None:
        # Credentials are used for precisely this configured URL. Redirects
        # are disabled and no authenticated follow-up/spider requests occur.
        scheme, secret = private_auth
        if (scheme not in ("bearer", "api_key") or not isinstance(secret, str)
                or not secret or len(secret) > 4096 or "\r" in secret or "\n" in secret):
            raise DiscoveryError("Invalid authorized catalog credential")
        headers["Authorization" if scheme == "bearer" else "X-API-Key"] = (
            "Bearer " + secret if scheme == "bearer" else secret)
    elif token and urllib.parse.urlsplit(url).hostname == "api.github.com":
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


def normalize_card(card, source_url: str, source: str, *, identity_salt: str = ""):
    if not isinstance(card, dict):
        return None
    name = clean_text(card.get("name"), 160)
    skills = card.get("skills", [])
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
    # A shared MCP endpoint can serve many distinct opaque resources. Keep
    # source_url as the actual HTTPS MCP endpoint, but distinguish records
    # without a public A2A endpoint using the opaque resource identity.
    stable = endpoint or (source_url + "|" + identity_salt if identity_salt else source_url)
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


def extract_registry_items(document, *, offset: int = 0, limit: int = MAX_CANDIDATES):
    """Take a bounded, rotating window from public JSON arrays.

    Rotating only explicitly configured bulk feeds keeps older entries from
    monopolizing the published snapshot; durable registry upserts accumulate
    the windows across days without deleting previously seen agents.
    """
    if isinstance(document, list):
        records = document
    elif isinstance(document, dict):
        records = None
        for key in ("agents", "items", "results", "entries", "data"):
            if isinstance(document.get(key), list):
                records = document[key]
                break
        if records is None and isinstance(document.get("data"), dict):
            return extract_registry_items(document["data"], offset=offset, limit=limit)
    else:
        records = None
    if not records:
        return []
    size = min(max(0, limit), len(records))
    offset %= len(records)
    if offset == 0:
        return records[:size]
    return [records[(offset + i) % len(records)] for i in range(size)]


def public_card_link(value: str, base_url: str, *, explicit_card: bool = False) -> str:
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
        if not explicit_card and not parsed.path.lower().endswith(".json"):
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
                "agent_card_uri", "agent_card", "card"):
        value = item.get(key)
        if isinstance(value, str):
            link = public_card_link(value, source_url, explicit_card=True)
            if link:
                urls.append(link)
    # Some manifests use {protocol:"a2a", url:".../card.json"}.
    protocol = str(item.get("protocol", "")).lower()
    if protocol == "a2a" or protocol == "agent2agent":
        link = public_card_link(item.get("url"), source_url)
        if link:
            urls.append(link)
    return list(dict.fromkeys(urls))


def unpack_cards(document, source_url: str, source: str, *, offset: int = 0,
                 inline_depth: int = 0):
    """Index A2A cards from manifests, typed catalogs, WebFinger, and ERC-8004.

    Typed non-A2A artifacts must never be misclassified as cards. Nested inline
    catalogs are strictly bounded; external document traversal remains one hop.
    """
    entries = document.get("entries") if isinstance(document, dict) else None
    is_ai_catalog = (isinstance(entries, list) and (
        isinstance(document.get("specVersion"), str) or
        any(isinstance(item, dict) and "mediaType" in item
            for item in entries[:MAX_MANIFEST_ITEMS])))
    special_document = (is_ai_catalog or (isinstance(document, dict) and
                        document.get("type") == "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"))
    direct = None if special_document else normalize_card(document, source_url, source)
    if direct:
        return [direct], []
    cards, links = [], []
    if isinstance(document, dict):
        # AI Catalog 1.0 is typed. Only A2A entries, not MCP/model/skill
        # artifacts, contribute Agent Cards or card URLs.
        if is_ai_catalog:
            nested = 0
            for entry in extract_registry_items(entries, offset=offset, limit=MAX_MANIFEST_ITEMS):
                if not isinstance(entry, dict):
                    continue
                media = clean_text(entry.get("mediaType"), 100).lower()
                if media in A2A_CATALOG_TYPES:
                    embedded = entry.get("data")
                    card = normalize_card(embedded, source_url, source)
                    if card:
                        cards.append(card)
                    ref = public_card_link(entry.get("url"), source_url,
                                           explicit_card=True)
                    if ref:
                        links.append(ref)
                elif media == "application/ai-catalog+json":
                    ref = public_card_link(entry.get("url"), source_url,
                                           explicit_card=True)
                    if ref:
                        links.append(ref)
                    elif (inline_depth == 0 and nested < MAX_INLINE_NESTED_CATALOGS
                          and isinstance(entry.get("data"), dict)):
                        nested += 1
                        child_cards, child_links = unpack_cards(
                            entry["data"], source_url, source, inline_depth=1)
                        cards.extend(child_cards)
                        links.extend(child_links)

        # Only operator-configured WebFinger account queries are sent. A JRD
        # may advertise cards via self links; arbitrary hrefs are ignored.
        wf_links = document.get("links")
        if isinstance(wf_links, list) and isinstance(document.get("subject"), str):
            for link in wf_links[:MAX_MANIFEST_ITEMS]:
                if not isinstance(link, dict):
                    continue
                if (link.get("rel") == "self" and
                        clean_text(link.get("type"), 100).lower() in A2A_CATALOG_TYPES):
                    ref = public_card_link(link.get("href"), source_url,
                                           explicit_card=True)
                    if ref:
                        links.append(ref)

        # ERC-8004 registration documents are *not* Agent Cards. Their
        # service pointers are followed only for the explicit A2A service.
        if document.get("type") == "https://eips.ethereum.org/EIPS/eip-8004#registration-v1":
            services = document.get("services")
            if isinstance(services, list):
                for service in services[:MAX_MANIFEST_ITEMS]:
                    if isinstance(service, dict) and service.get("name") == "A2A":
                        ref = public_card_link(service.get("endpoint"), source_url,
                                               explicit_card=True)
                        if ref:
                            links.append(ref)

    # AI Catalog entries are typed and must never be treated as arbitrary
    # registry rows (which might accidentally publish non-A2A artifacts).
    if not is_ai_catalog:
        for item in extract_registry_items(document, offset=offset, limit=MAX_MANIFEST_ITEMS):
            if not isinstance(item, dict):
                continue
            embedded = item.get("card") or item.get("agentCard") or item
            # Google Agent Registry returns a typed Card {type, content}.
            # Only A2A_AGENT_CARD content is a candidate; MCP/NO_SPEC is not.
            if isinstance(embedded, dict) and embedded.get("type") == "A2A_AGENT_CARD":
                content = embedded.get("content")
                if isinstance(content, str) and len(content) <= MAX_DOCUMENT:
                    try:
                        content = json.loads(content)
                    except (ValueError, TypeError):
                        content = None
                embedded = content if isinstance(content, dict) else None
            elif isinstance(embedded, dict) and "type" in embedded and "content" in embedded:
                embedded = None
            normalized = normalize_card(embedded, source_url, source)
            if normalized:
                cards.append(normalized)
            links.extend(card_references(item, source_url))
    return cards, list(dict.fromkeys(links))[:MAX_MANIFEST_ITEMS]


def domain_sources(config):
    """Probe only explicitly configured domains; no wildcard, port or tenant guessing."""
    sources = []
    assistants = config.get("assistant_ids", {})
    if not isinstance(assistants, dict):
        assistants = {}
    webfinger = config.get("webfinger_accounts", {})
    if not isinstance(webfinger, dict):
        webfinger = {}
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
        names = webfinger.get(origin, [])
        if isinstance(names, list):
            for name in names[:MAX_WEBFINGER_ACCOUNTS]:
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,60}", name):
                    resource = "acct:" + name + "@" + parsed.hostname
                    sources.append((origin + "/.well-known/webfinger?" +
                                    urllib.parse.urlencode({"resource": resource}),
                                    "webfinger"))
    return sources


def dns_txt_sources(config, fetch=fetch_json):
    """Follow configured NAIS _agent TXT pointers via public DNS-over-HTTPS.

    Discovery is not NAIS validation: DNS records and card signatures are
    not verified here. This method never publishes an identity assurance.
    """
    candidates, warnings = [], []
    domains = config.get("dns_domains", [])
    if not isinstance(domains, list):
        return candidates, warnings
    for value in domains[:MAX_DNS_DOMAINS]:
        if not isinstance(value, str) or len(value) > 240:
            continue
        domain = value.lower().rstrip(".")
        if not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
                domain):
            continue
        name = "_agent." + domain
        lookup = "https://dns.google/resolve?" + urllib.parse.urlencode(
            {"name": name, "type": "TXT"})
        try:
            response = fetch(lookup)
            if not isinstance(response, dict) or response.get("Status") != 0:
                continue
            answers = response.get("Answer", [])
            if not isinstance(answers, list):
                continue
            for item in answers[:MAX_DNS_TXT_ANSWERS]:
                if (not isinstance(item, dict) or item.get("type") != 16 or
                        not isinstance(item.get("name"), str) or
                        item["name"].rstrip(".").lower() != name):
                    continue
                data = item.get("data")
                if not isinstance(data, str) or len(data) > 1200:
                    continue
                # DoH TXT answers may consist of adjacent quoted segments.
                segments = re.findall(r'"(?:\\.|[^"\\])*"', data)
                try:
                    value = ("".join(json.loads(part) for part in segments)
                             if segments else data if '"' not in data else "")
                except ValueError:
                    continue
                if len(value) > 1000:
                    continue
                parts = [part.strip() for part in value.split(";")]
                if not parts or parts[0].lower() != "v=nais1":
                    continue
                fields = dict(part.split("=", 1) for part in parts[1:]
                              if "=" in part and part.split("=", 1)[0] in ("manifest", "k"))
                url = (public_card_link(fields["manifest"], "https://" + domain,
                                        explicit_card=True)
                       if fields.get("manifest") else
                       "https://" + domain + "/.well-known/agent.json")
                if url:
                    candidates.append((url, "dns-nais"))
        except (DiscoveryError, ValueError, TypeError):
            # Configured public domains are non-secret; do not include TXT
            # payload contents in warning messages.
            warnings.append("DNS discovery unavailable")
    return list(dict.fromkeys(candidates)), warnings


def post_chain_rpc(url: str, method: str, params):
    """Bounded read-only Ethereum RPC; never send tasks or transactions."""
    if method not in ("eth_blockNumber", "eth_getLogs"):
        raise DiscoveryError("Unsupported chain RPC method")
    require_public_https(url)
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params}, separators=(",", ":")).encode("utf-8")
    if len(payload) > 3000:
        raise DiscoveryError("Oversized chain RPC request")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
               "Content-Type": "application/json"}
    try:
        with OPENER.open(urllib.request.Request(url, data=payload, headers=headers,
                                                method="POST"), timeout=7) as response:
            if response.status != 200:
                raise DiscoveryError("Chain RPC unavailable")
            media = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if media != "application/json" and not media.endswith("+json"):
                raise DiscoveryError("Chain RPC not JSON")
            body = response.read(MAX_DOCUMENT + 1)
            if len(body) > MAX_DOCUMENT:
                raise DiscoveryError("Oversized chain RPC response")
            result = json.loads(body)
            if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != 1:
                raise DiscoveryError("Invalid chain RPC response")
            return result
    except (ValueError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError("Chain RPC unavailable") from exc


def erc8004_event_uri(log, contract: str) -> str:
    """Extract the one non-indexed dynamic string in registered/updated logs."""
    if not isinstance(log, dict) or log.get("removed") is True:
        return ""
    if not isinstance(log.get("address"), str) or log["address"].lower() != contract.lower():
        return ""
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != 3 or not isinstance(topics[0], str):
        return ""
    if topics[0].lower() not in (REGISTERED_TOPIC, URI_UPDATED_TOPIC):
        return ""
    for value in topics[1:]:
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
            return ""
    data = log.get("data")
    if not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){64,4096}", data):
        return ""
    try:
        raw = bytes.fromhex(data[2:])
        offset = int.from_bytes(raw[:32], "big")
        if offset != 32 or len(raw) < 64:
            return ""
        size = int.from_bytes(raw[32:64], "big")
        if size < 1 or size > 2048 or len(raw) < 64 + size:
            return ""
        uri = raw[64:64 + size].decode("utf-8")
        return uri if len(uri) <= 2048 else ""
    except (ValueError, UnicodeError):
        return ""


def erc8004_event_sources(config, post=post_chain_rpc):
    """Bounded, explicitly enabled log discovery, never agent execution.

    Registrations are pointers, not verified chain ownership, reputation or
    publisher identity. HTTPS registration files are fetched by normal crawl.
    """
    sources = config.get("erc8004_rpc_sources", [])
    candidates, warnings = [], []
    if not isinstance(sources, list):
        return candidates, warnings
    for entry in sources[:MAX_ERC8004_RPC_SOURCES]:
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        rpc = entry.get("rpc_url")
        address = entry.get("registry_address")
        if (not isinstance(rpc, str) or not isinstance(address, str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address)):
            warnings.append("ERC-8004 source misconfigured")
            continue
        fixed_start = entry.get("from_block")
        fixed_end = entry.get("to_block")
        if (fixed_start is None) != (fixed_end is None):
            warnings.append("ERC-8004 source misconfigured")
            continue
        try:
            if fixed_start is None:
                lookback = entry.get("lookback_blocks", 128)
                if type(lookback) is not int or not 1 <= lookback <= MAX_ERC8004_BLOCK_WINDOW:
                    raise DiscoveryError("Invalid lookback")
                status = post(rpc, "eth_blockNumber", [])
                latest = status.get("result") if isinstance(status, dict) else None
                if not isinstance(latest, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", latest):
                    raise DiscoveryError("Invalid chain tip")
                end = int(latest, 16)
                start = max(0, end - lookback + 1)
            else:
                if (type(fixed_start) is not int or type(fixed_end) is not int
                        or fixed_start < 0 or fixed_end < fixed_start
                        or fixed_end - fixed_start >= MAX_ERC8004_BLOCK_WINDOW):
                    raise DiscoveryError("Invalid block bounds")
                start, end = fixed_start, fixed_end
            reply = post(rpc, "eth_getLogs", [{
                "address": address,
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "topics": [[REGISTERED_TOPIC, URI_UPDATED_TOPIC]],
            }])
            logs = reply.get("result") if isinstance(reply, dict) else None
            if not isinstance(logs, list):
                raise DiscoveryError("Invalid log response")
            if len(logs) > MAX_ERC8004_LOGS:
                warnings.append("ERC-8004 log window truncated")
            # eth_getLogs returns logs in chain order. A later URIUpdated
            # supersedes Registered for the same agent within this window.
            latest_by_agent = {}
            for log in logs[:MAX_ERC8004_LOGS]:
                uri = erc8004_event_uri(log, address)
                link = (public_card_link(uri, rpc, explicit_card=True)
                        if uri.startswith("https://") else "")
                if link:
                    latest_by_agent[log["topics"][1].lower()] = link
            candidates.extend((link, "erc8004-registration")
                              for link in latest_by_agent.values())
        except (DiscoveryError, ValueError, TypeError):
            # RPC URLs may contain API keys. Never include them in warnings.
            warnings.append("ERC-8004 source unavailable")
    return list(dict.fromkeys(candidates)), warnings


def erc8004_checkpointed_sources(config, state: dict, post=post_chain_rpc):
    """Resume sequential confirmed ERC-8004 block windows without skipping gaps.

    State is a staged, sanitized copy of the last committed checkpoint.
    The caller persists it only after a successful authenticated ingest.
    A failed or oversized RPC response never advances the block cursor.
    """
    candidates, warnings = [], []
    entries = config.get("erc8004_rpc_sources", [])
    if not isinstance(entries, list) or not isinstance(state, dict):
        return candidates, warnings
    checkpoints = state.setdefault("erc8004", {})
    if not isinstance(checkpoints, dict):
        raise DiscoveryError("Invalid ERC-8004 checkpoint state")
    for source in entries[:MAX_ERC8004_RPC_SOURCES]:
        if not isinstance(source, dict) or source.get("enabled") is not True:
            continue
        rpc, address = source.get("rpc_url"), source.get("registry_address")
        if (not isinstance(rpc, str) or not isinstance(address, str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address)):
            warnings.append("ERC-8004 source misconfigured")
            continue
        # Explicit fixed blocks remain available for one-off/manual probes.
        # They do not mutate a persistent sequential cursor.
        if source.get("from_block") is not None or source.get("to_block") is not None:
            found, errors = erc8004_event_sources(
                {"erc8004_rpc_sources": [source]}, post=post)
            candidates.extend(found)
            warnings.extend(errors)
            continue
        lookback = source.get("lookback_blocks", 512)
        finality = source.get("finality_blocks", 64)
        first = source.get("start_block")
        configured_window = source.get("window_blocks", MAX_ERC8004_CHECKPOINT_WINDOW)
        if (type(lookback) is not int or not 1 <= lookback <= MAX_ERC8004_CHECKPOINT_WINDOW
                or type(finality) is not int or not 0 <= finality <= 4096
                or first is not None and (type(first) is not int or not 0 <= first <= 2**60)
                or type(configured_window) is not int
                or not 1 <= configured_window <= MAX_ERC8004_CHECKPOINT_WINDOW):
            warnings.append("ERC-8004 source misconfigured")
            continue
        # Hash the configured RPC locator: provider API keys and credentials
        # must never appear in a public checkpoint branch.
        key = hashlib.sha256((rpc + "|" + address.lower()).encode()).hexdigest()[:32]
        previous = checkpoints.get(key, {})
        if not isinstance(previous, dict):
            raise DiscoveryError("Invalid ERC-8004 cursor record")
        prior_block = previous.get("next_block")
        prior_span = previous.get("window")
        if prior_block is not None and (
                type(prior_block) is not int or not 0 <= prior_block <= 2**60):
            raise DiscoveryError("Invalid ERC-8004 cursor height")
        if prior_span is not None and (
                type(prior_span) is not int or not 1 <= prior_span <= MAX_ERC8004_CHECKPOINT_WINDOW):
            raise DiscoveryError("Invalid ERC-8004 cursor window")
        try:
            status = post(rpc, "eth_blockNumber", [])
            latest = status.get("result") if isinstance(status, dict) else None
            if not isinstance(latest, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,16}", latest):
                raise DiscoveryError("Invalid chain tip")
            end = int(latest, 16) - finality
            if end < 0:
                continue
            cursor = (prior_block if prior_block is not None else
                      first if first is not None else max(0, end - lookback + 1))
            window = prior_span if prior_span is not None else configured_window
            # Keep the exact bootstrap height even if the very first log
            # request fails. It is staged, not persisted until ingest succeeds.
            checkpoints[key] = {"next_block": cursor, "window": window}
            # Shrink for provider limits in this run only. A previously small
            # persisted span must be allowed to grow after a quiet interval.
            adaptive_ceiling = configured_window
            completed = 0
            attempts = 0
            per_agent = {}
            while (cursor <= end and completed < MAX_ERC8004_WINDOWS_PER_RUN
                   and attempts < MAX_ERC8004_ATTEMPTS_PER_RUN):
                to_block = min(end, cursor + window - 1)
                attempts += 1
                try:
                    reply = post(rpc, "eth_getLogs", [{
                        "address": address, "fromBlock": hex(cursor),
                        "toBlock": hex(to_block),
                        "topics": [[REGISTERED_TOPIC, URI_UPDATED_TOPIC]],
                    }])
                except DiscoveryError:
                    # Provider range limits apply to every window, not just
                    # early attempts. Keep the narrower cap for this run.
                    if window > 1:
                        window = max(1, window // 2)
                        adaptive_ceiling = min(adaptive_ceiling, window)
                        continue
                    raise
                logs = reply.get("result") if isinstance(reply, dict) else None
                if not isinstance(logs, list):
                    # Provider overload/errors may be block-range limits.
                    # Narrow and retry; never mark unseen blocks complete.
                    if window > 1:
                        window = max(1, window // 2)
                        adaptive_ceiling = min(adaptive_ceiling, window)
                        continue
                    raise DiscoveryError("Invalid chain log response")
                if len(logs) >= MAX_ERC8004_LOGS:
                    if window > 1:
                        window = max(1, window // 2)
                        adaptive_ceiling = min(adaptive_ceiling, window)
                        continue
                    warnings.append("ERC-8004 single block exceeds bounded log budget")
                    break
                tentative = dict(per_agent)
                for log in logs:
                    uri = erc8004_event_uri(log, address)
                    link = (public_card_link(uri, rpc, explicit_card=True)
                            if uri.startswith("https://") else "")
                    if link:
                        tentative[log["topics"][1].lower()] = link
                if len(tentative) > MAX_ERC8004_CHECKPOINT_URLS_PER_SOURCE:
                    if window > 1:
                        window = max(1, window // 2)
                        adaptive_ceiling = min(adaptive_ceiling, window)
                        continue
                    warnings.append("ERC-8004 URL budget reached; cursor retained")
                    break
                per_agent = tentative
                cursor = to_block + 1
                completed += 1
                # Grow conservatively after a successful sparse window.
                if len(logs) < MAX_ERC8004_LOGS // 4:
                    window = min(adaptive_ceiling, window * 2)
            checkpoints[key] = {"next_block": cursor, "window": window}
            candidates.extend((url, "erc8004-registration")
                              for url in per_agent.values())
            if cursor <= end:
                warnings.append("ERC-8004 backfill continues at saved cursor")
        except (DiscoveryError, ValueError, TypeError, RecursionError):
            # A partial successful prefix is not silently committed on error:
            # retry the entire source scan next scheduled run.
            warnings.append("ERC-8004 source unavailable")
    return list(dict.fromkeys(candidates)), warnings


def fetch_oci_bytes(url: str, auth: str = "") -> bytes:
    """GET an OCI manifest or digest-verified blob without redirects or leaks."""
    require_public_https(url)
    if not isinstance(auth, str) or len(auth) > 4096 or "\n" in auth or "\r" in auth:
        raise DiscoveryError("Invalid OCI credential")
    is_blob = "/blobs/sha256:" in urllib.parse.urlsplit(url).path
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": ("application/octet-stream" if is_blob else
                   "application/vnd.oci.image.manifest.v1+json, application/json"),
    }
    if auth:
        headers["Authorization"] = "Bearer " + auth
    try:
        req = urllib.request.Request(url, headers=headers)
        with OPENER.open(req, timeout=7) as response:
            if response.status != 200:
                raise DiscoveryError("OCI source unavailable")
            media = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if (media != "application/json" and
                    not (media.startswith("application/") and media.endswith("+json")) and
                    not (is_blob and media == "application/octet-stream")):
                raise DiscoveryError("Unexpected OCI response media type")
            data = response.read(MAX_OCI_DOCUMENT + 1)
            if len(data) > MAX_OCI_DOCUMENT:
                raise DiscoveryError("Oversized OCI artifact")
            return data
    except (ValueError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError("OCI source unavailable") from exc


def oasf_a2a_cards(record, source_url: str):
    """Only typed integration/a2a artifacts in AGNTCY OASF records are indexed."""
    cards, links = [], []
    if not isinstance(record, dict) or not isinstance(record.get("modules"), list):
        return cards, links
    for module in record["modules"][:MAX_OCI_LAYERS * 4]:
        if not isinstance(module, dict):
            continue
        meta = module.get("module") if isinstance(module.get("module"), dict) else module
        if meta.get("name") != "integration/a2a":
            continue
        artifact = module.get("artifact") or meta.get("artifact")
        if isinstance(artifact, dict) and (
                artifact.get("media_type") or artifact.get("mediaType")) in A2A_CATALOG_TYPES:
            inline = artifact.get("json")
            if not isinstance(inline, dict):
                data = artifact.get("data")
                if isinstance(data, str) and len(data) <= MAX_OCI_DOCUMENT:
                    try:
                        decoded = base64.b64decode(data, validate=True)
                        if len(decoded) <= MAX_DOCUMENT:
                            inline = json.loads(decoded)
                    except (ValueError, binascii.Error, UnicodeError):
                        inline = None
            card = normalize_card(inline, source_url, "agntcy-oci")
            if card:
                card.pop("raw_card", None)
                cards.append(card)
            # OASF descriptor.json uses `urls`, not a singular `url`.
            candidates = artifact.get("urls")
            if not isinstance(candidates, list):
                candidates = []
            # Tolerate earlier singular exports without allowing arbitrary
            # recursive requests or leaking configured registry credentials.
            if isinstance(artifact.get("url"), str):
                candidates = [artifact["url"], *candidates]
            for candidate in candidates[:MAX_OCI_LAYERS * 2]:
                if isinstance(candidate, str) and candidate.startswith("https://"):
                    link = public_card_link(candidate, source_url, explicit_card=True)
                    if link:
                        links.append(link)
        # Deprecated OASF a2a_data.card_data is the serialized field name.
        data = module.get("data") or meta.get("data")
        if isinstance(data, dict):
            old_card = data.get("card_data")
            if not isinstance(old_card, dict):
                old_card = data.get("a2a_card_data")
            if isinstance(old_card, dict):
                card = normalize_card(old_card, source_url, "agntcy-oci")
                if card:
                    card.pop("raw_card", None)
                    cards.append(card)
    return cards, list(dict.fromkeys(links))


def agntcy_oci_sources(config, read=fetch_oci_bytes):
    """Read configured, digest-checked OCI Directory manifests and OASF layers.

    Only explicitly approved repositories publish normalized agent metadata.
    Tags, OCI authentication discovery, DHT gossip and untrusted container
    execution are intentionally out of scope.
    """
    sources = config.get("agntcy_oci_sources", [])
    cards, links, warnings = [], [], []
    if not isinstance(sources, list):
        return cards, links, warnings
    for entry in sources[:MAX_OCI_SOURCES]:
        if not isinstance(entry, dict) or entry.get("publish") is not True:
            continue
        origin = entry.get("registry_origin")
        repo = entry.get("repository")
        digest = entry.get("manifest_digest")
        auth = entry.get("token", "")
        if not all(isinstance(x, str) for x in (origin, repo, digest, auth)):
            warnings.append("AGNTCY OCI source misconfigured")
            continue
        try:
            parsed = urllib.parse.urlsplit(origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.port not in (None, 443)
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                    or not 1 <= len(repo) <= 200 or not re.fullmatch(
                        r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*",
                        repo)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
                raise DiscoveryError("Unsupported OCI source")
            base = origin.rstrip("/") + "/v2/" + repo
            manifest_url = base + "/manifests/" + digest
            raw = read(manifest_url, auth)
            if not isinstance(raw, bytes) or len(raw) > MAX_OCI_DOCUMENT:
                raise DiscoveryError("Invalid OCI manifest")
            if hashlib.sha256(raw).hexdigest() != digest[7:]:
                raise DiscoveryError("OCI manifest digest mismatch")
            manifest = json.loads(raw)
            if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2
                    or manifest.get("artifactType") !=
                    "application/vnd.agntcy.dir.record.v1+json"
                    or not isinstance(manifest.get("layers"), list)):
                raise DiscoveryError("Not an AGNTCY Directory artifact")
            source_cards, source_links = [], []
            reconstructed = None
            for index, layer in enumerate(manifest["layers"][:MAX_OCI_LAYERS]):
                if not isinstance(layer, dict):
                    continue
                media = layer.get("mediaType")
                if not isinstance(media, str) or not media.startswith(
                        "application/vnd.agntcy.oasf.types."):
                    continue
                # The first layer is the base Record. Component Module layers
                # hold the actual A2A artifacts in normal OCI exports.
                is_record = media.endswith(".Record+json")
                is_module = media.endswith(".Module+json")
                if not (is_record or is_module):
                    continue
                if index == 0 and not is_record:
                    raise DiscoveryError("OCI base layer must be Record")
                layer_digest = layer.get("digest")
                if not isinstance(layer_digest, str) or not re.fullmatch(
                        r"sha256:[0-9a-f]{64}", layer_digest):
                    raise DiscoveryError("Invalid OCI layer digest")
                inline = layer.get("data")
                if inline is not None:
                    if not isinstance(inline, str) or len(inline) > MAX_OCI_DOCUMENT:
                        raise DiscoveryError("Oversized inline OCI layer")
                    try:
                        body = base64.b64decode(inline, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise DiscoveryError("Invalid inline OCI layer") from exc
                else:
                    body = read(base + "/blobs/" + layer_digest, auth)
                if not isinstance(body, bytes) or len(body) > MAX_OCI_DOCUMENT:
                    raise DiscoveryError("Invalid OCI layer")
                if hashlib.sha256(body).hexdigest() != layer_digest[7:]:
                    raise DiscoveryError("OCI layer digest mismatch")
                component = json.loads(body)
                if not isinstance(component, dict):
                    raise DiscoveryError("Invalid OASF component data")
                if is_record:
                    if index != 0 or reconstructed is not None:
                        raise DiscoveryError("Duplicate base OASF Record")
                    reconstructed = component
                    if not isinstance(reconstructed.get("modules", []), list):
                        raise DiscoveryError("Invalid base Record modules")
                    reconstructed["modules"] = reconstructed.get("modules", [])[:MAX_OCI_LAYERS * 4]
                elif is_module:
                    if reconstructed is None:
                        raise DiscoveryError("OASF module without base Record")
                    annotations = layer.get("annotations")
                    if not isinstance(annotations, dict):
                        annotations = {}
                    # OCI layer descriptors carry the module name/id, unlike
                    # the small JSON Module blob. Only typed integration/a2a
                    # contributes Agent Cards; metadata remains untrusted.
                    name = annotations.get("agntcy.oasf.module/name")
                    if isinstance(name, str):
                        component["name"] = name
                    module_id = annotations.get("agntcy.oasf.module/id")
                    if isinstance(module_id, str):
                        component["id"] = module_id
                    reconstructed["modules"].append(component)
            if reconstructed is None:
                raise DiscoveryError("OASF base Record missing")
            source_cards, source_links = oasf_a2a_cards(reconstructed, manifest_url)
            cards.extend(source_cards)
            links.extend(source_links)
        except (DiscoveryError, ValueError, TypeError, OverflowError, UnicodeError):
            # A private registry URL, repository and token are not public data.
            warnings.append("AGNTCY OCI source unavailable")
    return cards, list(dict.fromkeys(links))[:MAX_FOLLOWUP_CARD_URLS], warnings


def post_mcp_metadata(url: str, method: str, params, token: str = ""):
    """Call only 2026-07-28 MCP metadata methods, never tools or tasks."""
    if method not in ("resources/list", "resources/read"):
        raise DiscoveryError("Disallowed MCP method")
    require_public_https(url)
    if not isinstance(token, str) or len(token) > 4096 or "\r" in token or "\n" in token:
        raise DiscoveryError("Invalid MCP credential")
    if not isinstance(params, dict) or set(params) - {"cursor", "uri"}:
        raise DiscoveryError("Invalid MCP parameters")
    if method == "resources/read" and (
            not isinstance(params.get("uri"), str) or len(params["uri"]) > 1024):
        raise DiscoveryError("Invalid MCP resource")
    rpc_id = 1 if method == "resources/list" else 2
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method,
               "params": {**params, "_meta": {
                   "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                   "io.modelcontextprotocol/clientCapabilities": {},
                   "io.modelcontextprotocol/clientInfo": {
                       "name": "SearchA2A", "version": "0.1.0"}}}}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > 2048:
        raise DiscoveryError("Oversized MCP request")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
        "User-Agent": USER_AGENT,
    }
    if method == "resources/read":
        if not params["uri"].isascii() or any(ord(ch) < 32 for ch in params["uri"]):
            raise DiscoveryError("Unsupported MCP resource name")
        headers["Mcp-Name"] = params["uri"]
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with OPENER.open(urllib.request.Request(
                url, data=body, headers=headers, method="POST"), timeout=7) as response:
            if response.status != 200:
                raise DiscoveryError("MCP source unavailable")
            media = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            # SSE is intentionally unsupported: an unbounded stream must not
            # be treated as a small, deterministic discovery result.
            if media != "application/json" and not media.endswith("+json"):
                raise DiscoveryError("MCP response is not bounded JSON")
            data = response.read(MAX_MCP_DOCUMENT + 1)
            if len(data) > MAX_MCP_DOCUMENT:
                raise DiscoveryError("Oversized MCP response")
            result = json.loads(data)
            if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                    or result.get("id") != rpc_id or not isinstance(result.get("result"), dict)
                    or result.get("error") is not None):
                raise DiscoveryError("Invalid MCP response")
            return result["result"]
    except (ValueError, RecursionError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError("MCP source unavailable") from exc


def mcp_resource_card_uri(value: str) -> bool:
    """Allow only agent-specific opaque URIs, never filesystem or HTTP URLs."""
    if not isinstance(value, str) or len(value) > 1024 or not value.isascii():
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        return (parsed.scheme in ("a2a", "agent", "agents", "agent-card", "mcp")
                and not parsed.username and not parsed.password
                and not parsed.fragment and bool(parsed.netloc or parsed.path))
    except ValueError:
        return False


def mcp_metadata_sources(config, post=post_mcp_metadata):
    """Read explicitly approved typed A2A resources from stateless MCP APIs."""
    sources = config.get("mcp_sources", [])
    cards, links, warnings = [], [], []
    if not isinstance(sources, list):
        return cards, links, warnings
    for entry in sources[:MAX_MCP_SOURCES]:
        if not isinstance(entry, dict) or entry.get("publish") is not True:
            continue
        endpoint = entry.get("endpoint")
        token = entry.get("token", "")
        if not isinstance(endpoint, str) or not isinstance(token, str):
            warnings.append("MCP discovery misconfigured")
            continue
        try:
            seen_cursors, seen_resources = set(), set()
            next_cursor = ""
            reads = 0
            local_cards, local_links = [], []
            for _ in range(MAX_MCP_PAGES):
                params = {"cursor": next_cursor} if next_cursor else {}
                response = post(endpoint, "resources/list", params, token)
                resources = response.get("resources") if isinstance(response, dict) else None
                if not isinstance(resources, list):
                    raise DiscoveryError("Invalid MCP resource list")
                for resource in resources[:MAX_MCP_RESOURCES_PER_PAGE]:
                    if not isinstance(resource, dict):
                        continue
                    mime = clean_text(resource.get("mimeType"), 100).lower()
                    uri = resource.get("uri")
                    if mime not in A2A_CATALOG_TYPES or not isinstance(uri, str):
                        continue
                    if uri.startswith("https://"):
                        link = public_card_link(uri, endpoint, explicit_card=True)
                        if link:
                            local_links.append(link)
                    elif (entry.get("allow_resource_read") is True and
                          reads < MAX_MCP_RESOURCE_READS and
                          mcp_resource_card_uri(uri) and uri not in seen_resources):
                        seen_resources.add(uri)
                        reads += 1
                        try:
                            response_card = post(endpoint, "resources/read", {"uri": uri}, token)
                        except (DiscoveryError, ValueError, TypeError, RecursionError):
                            # One stale or broken resource must not discard
                            # other discoveries from the same MCP directory.
                            continue
                        contents = response_card.get("contents") if isinstance(response_card, dict) else None
                        if not isinstance(contents, list):
                            continue
                        for part in contents[:4]:
                            if (not isinstance(part, dict)
                                    or clean_text(part.get("mimeType"), 100).lower()
                                    not in A2A_CATALOG_TYPES):
                                continue
                            text = part.get("text")
                            if not isinstance(text, str) or len(text) > MAX_MCP_DOCUMENT:
                                continue
                            try:
                                card = normalize_card(json.loads(text), endpoint, "mcp-resource",
                                                      identity_salt=uri)
                            except (ValueError, TypeError, RecursionError):
                                card = None
                            if card:
                                card.pop("raw_card", None)
                                local_cards.append(card)
                cursor = response.get("nextCursor")
                if (not isinstance(cursor, str) or not 1 <= len(cursor) <= 256
                        or not cursor.isascii() or cursor in seen_cursors):
                    break
                seen_cursors.add(cursor)
                next_cursor = cursor
            cards.extend(local_cards)
            links.extend(local_links)
        except (DiscoveryError, ValueError, TypeError, RecursionError):
            # Configured MCP hosts and Bearer secrets may be private; no leaks.
            warnings.append("MCP discovery unavailable")
    return cards, list(dict.fromkeys(links))[:MAX_FOLLOWUP_CARD_URLS], warnings


def post_public_directory_discover(url: str, need: str):
    """Make one bounded metadata-only directory search, not an A2A task."""
    if url not in PUBLIC_DISCOVERY_ENDPOINTS or need not in PUBLIC_DISCOVERY_TOPICS:
        raise DiscoveryError("Unsupported public directory request")
    require_public_https(url)
    body = json.dumps({"need": need, "alive": True,
                       "protocols": ["A2A"], "endpoint": "a2a",
                       "limit": MAX_PUBLIC_DISCOVERY_ROWS},
                      separators=(",", ":")).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
               "Content-Type": "application/json"}
    try:
        with OPENER.open(urllib.request.Request(
                url, data=body, headers=headers, method="POST"), timeout=7) as response:
            if response.status != 200:
                raise DiscoveryError("Directory metadata unavailable")
            media = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if media != "application/json" and not (
                    media.startswith("application/") and media.endswith("+json")):
                raise DiscoveryError("Directory metadata is not JSON")
            data = response.read(MAX_PUBLIC_DISCOVERY_BYTES + 1)
            if len(data) > MAX_PUBLIC_DISCOVERY_BYTES:
                raise DiscoveryError("Oversized directory metadata")
            return json.loads(data)
    except (ValueError, RecursionError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError("Directory metadata unavailable") from exc


def public_directory_sources(config, post=post_public_directory_discover):
    """Harvest only card metadata from explicitly enabled public search APIs.

    Search queries are fixed, non-personal topical strings. A daily three-topic
    rotation offers broad recall without costs or remote task execution.
    """
    configured = config.get("public_directory_apis", [])
    cards, links, warnings = [], [], []
    if not isinstance(configured, list):
        return cards, links, warnings
    rotation = config.get("rotation_index")
    if type(rotation) is not int or rotation < 0:
        rotation = datetime.now(timezone.utc).date().toordinal()
    for entry in configured[:MAX_PUBLIC_DISCOVERY_APIS]:
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        endpoint = entry.get("endpoint")
        if endpoint not in PUBLIC_DISCOVERY_ENDPOINTS:
            warnings.append("Public directory source unsupported")
            continue
        local_links = []
        for i in range(MAX_PUBLIC_DISCOVERY_TOPICS_PER_RUN):
            topic = PUBLIC_DISCOVERY_TOPICS[
                (rotation * MAX_PUBLIC_DISCOVERY_TOPICS_PER_RUN + i)
                % len(PUBLIC_DISCOVERY_TOPICS)]
            try:
                document = post(endpoint, topic)
                if isinstance(document, dict) and isinstance(document.get("matches"), list):
                    rows = document["matches"][:MAX_PUBLIC_DISCOVERY_ROWS]
                else:
                    rows = extract_registry_items(document, limit=MAX_PUBLIC_DISCOVERY_ROWS)
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    refs = card_references(item, endpoint)
                    if refs:
                        local_links.extend(refs)
                        continue
                    # The directory's /discover URL is NOT an Agent Card URL.
                    # Derive only a canonical HTTPS card probe from an explicitly
                    # labeled A2A endpoint and let existing GET validation decide.
                    embedded = item.get("card") or item.get("agentCard")
                    services = item.get("endpoints")
                    reach = item.get("reach")
                    a2a = (embedded.get("url") if isinstance(embedded, dict) else None)
                    if not a2a and isinstance(services, dict):
                        a2a = services.get("a2a")
                    if (not a2a and isinstance(reach, dict)
                            and reach.get("type") == "a2a"):
                        a2a = reach.get("door")
                    if not isinstance(a2a, str):
                        continue
                    try:
                        parsed = urllib.parse.urlsplit(a2a)
                        if (parsed.scheme != "https" or not parsed.hostname
                                or parsed.username or parsed.password
                                or parsed.port not in (None, 443)):
                            continue
                        derived = public_card_link(
                            "https://" + parsed.hostname + "/.well-known/agent-card.json",
                            endpoint, explicit_card=True)
                    except ValueError:
                        continue
                    if derived:
                        local_links.append(derived)
            except (DiscoveryError, ValueError, TypeError, RecursionError):
                warnings.append("Public directory metadata unavailable")
        links.extend(local_links)
    return cards, list(dict.fromkeys(links))[:MAX_FOLLOWUP_CARD_URLS], warnings


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


def discover(query: str, config: dict, token: str = "", fetch=fetch_json, github=github_sources,
             private_fetch=fetch_json, dns_fetch=fetch_json, chain_rpc=post_chain_rpc,
             oci_read=fetch_oci_bytes, mcp_post=post_mcp_metadata,
             public_discover=post_public_directory_discover, chain_state=None,
             retry_state=None, health_report=None, retry_day=None):
    results = {}
    warnings = []
    candidates = candidate_sources(config)
    code_urls, search_warnings = github(query, token)
    warnings.extend(search_warnings)
    dns_cards, dns_warnings = dns_txt_sources(config, fetch=dns_fetch)
    candidates.extend(dns_cards)
    warnings.extend(dns_warnings)
    chain_cards, chain_warnings = (erc8004_checkpointed_sources(config, chain_state, post=chain_rpc)
                                  if chain_state is not None else
                                  erc8004_event_sources(config, post=chain_rpc))
    candidates.extend(chain_cards)
    warnings.extend(chain_warnings)
    oci_cards, oci_links, oci_warnings = agntcy_oci_sources(config, read=oci_read)
    for record in oci_cards:
        results.setdefault(record["id"], record)
    candidates.extend((link, "agntcy-oci") for link in oci_links)
    warnings.extend(oci_warnings)
    mcp_cards, mcp_links, mcp_warnings = mcp_metadata_sources(config, post=mcp_post)
    for record in mcp_cards:
        results.setdefault(record["id"], record)
    candidates.extend((link, "mcp-resource") for link in mcp_links)
    warnings.extend(mcp_warnings)
    # A directory search is metadata-only, enabled for scheduled indexing,
    # never used to service a live end-user query.
    if not query:
        public_cards, public_links, public_warnings = public_directory_sources(
            config, post=public_discover)
        for record in public_cards:
            results.setdefault(record["id"], record)
        candidates.extend((link, "public-directory") for link in public_links)
        warnings.extend(public_warnings)
    rotation = config.get('rotation_index')
    if type(rotation) is not int or rotation < 0:
        rotation = datetime.now(timezone.utc).date().toordinal()
    # Registry URLs are explicit curated seeds, not arbitrary user-provided URLs.
    for url in config.get("registry_urls", [])[:10]:
        try:
            # Large JSON dumps are allowed only at explicitly configured feed URLs.
            document = (fetch(url, max_bytes=MAX_PUBLIC_REGISTRY_JSON)
                        if fetch is fetch_json else fetch(url))
            offset = rotation * MAX_MANIFEST_ITEMS if not query and config.get("rotate_registry_feeds") else 0
            embedded, links = unpack_cards(document, url, "registry", offset=offset)
            for record in embedded:
                results.setdefault(record["id"], record)
            candidates.extend((link, "registry") for link in links)
        except DiscoveryError:
            warnings.append(f"Registry unavailable: {urllib.parse.urlsplit(url).hostname}")
    # Explicitly authorized feeds are opt-in for publication. Credentials are
    # delivered only to the configured feed URL and never passed to any linked
    # Agent Card, proxy, redirect or untrusted registry. The default is private.
    private_sources = config.get("private_catalogs", [])
    if isinstance(private_sources, list):
        for entry in private_sources[:MAX_PRIVATE_CATALOGS]:
            if not isinstance(entry, dict) or entry.get("publish") is not True:
                continue
            url = entry.get("url")
            scheme = entry.get("auth_type")
            secret = entry.get("token")
            if (not isinstance(url, str) or scheme not in ("bearer", "api_key")
                    or not isinstance(secret, str) or not secret):
                warnings.append("Authorized catalog configuration missing")
                continue
            try:
                is_gcp = entry.get("kind") == "gcp_agents_list"
                if is_gcp:
                    parsed = urllib.parse.urlsplit(url)
                    if (parsed.hostname != "agentregistry.googleapis.com"
                            or parsed.query or parsed.fragment or
                            not re.fullmatch(
                                r"/v1/projects/[A-Za-z0-9_.-]+/locations/[A-Za-z0-9_-]+/agents",
                                parsed.path)):
                        raise DiscoveryError("Unsupported authorized registry API")
                page_token = ""
                for _page in range(MAX_REGISTRY_PAGES if is_gcp else 1):
                    page_url = url
                    if is_gcp:
                        args = {"pageSize": REGISTRY_PAGE_SIZE}
                        if page_token:
                            args["pageToken"] = page_token
                        page_url += "?" + urllib.parse.urlencode(args)
                    if private_fetch is fetch_json:
                        document = private_fetch(page_url, max_bytes=MAX_PUBLIC_REGISTRY_JSON,
                                                 private_auth=(scheme, secret))
                    else:
                        document = private_fetch(page_url, scheme, secret)
                    # The stable source is the configured URL; a page token must
                    # never be included in public registry metadata.
                    embedded, links = unpack_cards(document, url, "authorized-catalog")
                    for record in embedded:
                        # Private raw metadata (potentially including credentials,
                        # prompts, or internal paths) must not be republished.
                        record.pop("raw_card", None)
                        results.setdefault(record["id"], record)
                    candidates.extend((link, "authorized-catalog") for link in links)
                    if not is_gcp or not isinstance(document, dict):
                        break
                    token_next = document.get("nextPageToken")
                    if not isinstance(token_next, str) or not token_next or len(token_next) > 1024:
                        break
                    page_token = token_next
            except (DiscoveryError, ValueError, TypeError):
                # Do not put private feed URLs or credentials into public warnings.
                warnings.append("Authorized catalog unavailable")

    # Public A2A registries expose Agent Card URLs through a paginated API.
    # Only request URLs derived from configured HTTPS registry origins, never
    # untrusted pagination links or an agent endpoint. Every card is fetched
    # and normalized through the same safety gate as the existing sources.
    for url in config.get("paginated_registry_urls", [])[:2]:
        if not isinstance(url, str):
            continue
        seen = 0
        base_offset = 0
        first_page = None
        separator = "&" if urllib.parse.urlsplit(url).query else "?"
        if not query and config.get("rotate_paginated_registry"):
            # One bounded first-page GET establishes total for a safe rotation.
            probe_url = url + separator + urllib.parse.urlencode(
                {"limit": REGISTRY_PAGE_SIZE, "offset": 0})
            try:
                first_page = fetch(probe_url)
                total = first_page.get("total") if isinstance(first_page, dict) else None
                if isinstance(total, int) and not isinstance(total, bool) and 0 < total <= 50000:
                    batches = (total + MAX_REGISTRY_CARD_URLS - 1) // MAX_REGISTRY_CARD_URLS
                    base_offset = (rotation % batches) * MAX_REGISTRY_CARD_URLS
            except (DiscoveryError, ValueError, TypeError):
                warnings.append(f"Registry unavailable: {urllib.parse.urlsplit(url).hostname}")
                continue
        for page in range(1 if query else MAX_REGISTRY_PAGES):
            if seen >= MAX_REGISTRY_CARD_URLS:
                break
            params = {"limit": REGISTRY_PAGE_SIZE,
                      "offset": base_offset + page * REGISTRY_PAGE_SIZE}
            if query:
                params["search"] = query
            page_url = url + separator + urllib.parse.urlencode(params)
            try:
                document = (first_page if page == 0 and base_offset == 0 and first_page is not None
                            else fetch(page_url))
                records = extract_registry_items(document)
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

    # Retry queues are public-only. Never save an authenticated catalog/OCI/MCP
    # URL or followup. Their source details may be commercially confidential.
    scheduled_retries = retry_state is not None and not query
    retry_outcomes = []
    eligible_public = {}
    retry_prefix, deferred = [], set()
    if scheduled_retries:
        crawl_retry.validate_retry_state(retry_state)
        today = (retry_day if retry_day is not None
                 else datetime.now(timezone.utc).date().toordinal())
        retry_prefix, deferred = crawl_retry.due_retries(retry_state, today)
        for url, origin in [*retry_prefix, *candidates, *code_urls]:
            if (origin in crawl_retry.PUBLIC_ORIGINS and
                    crawl_retry.safe_public_card_url(url)):
                eligible_public.setdefault(url, origin)
    github_cards = list(distinct_url(
        (url, origin) for url, origin in code_urls
        if url not in deferred))[:MAX_GITHUB_CANDIDATES]
    reserved = len(github_cards)
    unique = list(distinct_url(
        (url, origin) for url, origin in [*retry_prefix, *candidates]
        if url not in deferred or origin not in crawl_retry.PUBLIC_ORIGINS))[:MAX_CANDIDATES - reserved]
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
            return cards, links, "accepted" if cards or links else "invalid", pair
        except DiscoveryError as exc:
            # An HTML fallback or malformed JSON is not a transient offline
            # Agent Card. Don't keep retrying permanent non-card responses.
            malformed = (
                str(exc) in ("Non-JSON response Content-Type", "Oversized JSON") or
                str(exc).endswith(": JSONDecodeError"))
            return [], [], "invalid" if malformed else "unavailable", pair
        except (ValueError, TypeError, OverflowError, RecursionError):
            return [], [], "invalid", pair

    def prefer_verified(card):
        """Preserve a fresh publisher proof over an unverified registry copy.

        An embedded registry record can advertise the same endpoint as the
        original card, but has no authority to suppress the card's proof.
        """
        previous = results.get(card["id"])
        if previous is None or (
                card.get("publisher_verification") and
                not previous.get("publisher_verification")):
            results[card["id"]] = card

    discovered = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for cards, links, outcome, pair in pool.map(pull, unique):
            for record in cards:
                prefer_verified(record)
            discovered.extend((link, pair[1]) for link in links)
            if scheduled_retries and pair[0] in eligible_public:
                retry_outcomes.append((pair[0], eligible_public[pair[0]], outcome))

    # At most one further GET hop. Do not spider arbitrary links, invoke A2A
    # tasks/MCP tools, or let manifests override a publisher's own card.
    visited = {url for url, _ in unique}
    followups = []
    for url, parent_origin in discovered:
        if (url in deferred and parent_origin in crawl_retry.PUBLIC_ORIGINS):
            continue
        if url not in visited:
            visited.add(url)
            followups.append((url, "manifest"))
            # Private catalog descendants are not eligible for public persistence.
            if (scheduled_retries and parent_origin in crawl_retry.PUBLIC_ORIGINS
                    and crawl_retry.safe_public_card_url(url)):
                eligible_public.setdefault(url, "manifest")
            if len(followups) >= MAX_FOLLOWUP_CARD_URLS:
                break
    with ThreadPoolExecutor(max_workers=10) as pool:
        for cards, _links, outcome, pair in pool.map(pull, followups):
            for record in cards:
                prefer_verified(record)
            if scheduled_retries and pair[0] in eligible_public:
                retry_outcomes.append((pair[0], eligible_public[pair[0]], outcome))
    if scheduled_retries:
        metrics = crawl_retry.record_outcomes(retry_state, retry_outcomes, today)
        if health_report is not None:
            health_report.update({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "scope": "public-card-fetches-only",
                "due_selected": len(retry_prefix),
                "retry_deferred": len(deferred),
                **metrics,
            })
    ordered = sorted(results.values(), key=lambda r: (-rank(r, query), r["name"].lower()))
    if query:
        ordered = [r for r in ordered if rank(r, query) > 0]
    return {
        "query": query,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(ordered),
        "results": ordered,
        "warnings": warnings[:10],
        "verified": "Agent Card metadata only; endpoints are not actively probed",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--checkpoint-in")
    parser.add_argument("--checkpoint-out")
    parser.add_argument("--retry-in")
    parser.add_argument("--retry-out")
    parser.add_argument("--health-output")
    args = parser.parse_args()
    if len(args.query) > 120:
        parser.error("query exceeds 120 characters")
    config = json.loads((ROOT / "sources.json").read_text())
    private_sources = os.environ.get("SEARCHA2A_AUTHORIZED_CATALOGS_JSON", "")
    if private_sources:
        try:
            sources = json.loads(private_sources)
        except ValueError as exc:
            raise SystemExit("Invalid authorized catalog configuration JSON") from exc
        if not isinstance(sources, list):
            raise SystemExit("Authorized catalog configuration must be a JSON array")
        config["private_catalogs"] = sources
    chain_sources = os.environ.get("SEARCHA2A_ERC8004_RPC_SOURCES_JSON", "")
    if chain_sources:
        try:
            sources = json.loads(chain_sources)
        except ValueError as exc:
            raise SystemExit("Invalid ERC-8004 source configuration JSON") from exc
        if not isinstance(sources, list):
            raise SystemExit("ERC-8004 source configuration must be a JSON array")
        config["erc8004_rpc_sources"] = sources
    oci_sources = os.environ.get("SEARCHA2A_AGNTCY_OCI_SOURCES_JSON", "")
    if oci_sources:
        try:
            sources = json.loads(oci_sources)
        except ValueError as exc:
            raise SystemExit("Invalid AGNTCY OCI source configuration JSON") from exc
        if not isinstance(sources, list):
            raise SystemExit("AGNTCY OCI sources configuration must be a JSON array")
        config["agntcy_oci_sources"] = sources
    mcp_sources = os.environ.get("SEARCHA2A_MCP_SOURCES_JSON", "")
    if mcp_sources:
        try:
            sources = json.loads(mcp_sources)
        except ValueError as exc:
            raise SystemExit("Invalid MCP source configuration JSON") from exc
        if not isinstance(sources, list):
            raise SystemExit("MCP source configuration must be a JSON array")
        config["mcp_sources"] = sources
    if bool(args.checkpoint_in) != bool(args.checkpoint_out):
        parser.error("--checkpoint-in and --checkpoint-out are required together")
    chain_state = None
    if args.checkpoint_in:
        location = Path(args.checkpoint_in)
        if not location.exists():
            parser.error("Checkpoint file missing; run checkpoint load first")
        try:
            chain_state = json.loads(location.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            parser.error("Invalid checkpoint file")
        if (not isinstance(chain_state, dict) or chain_state.get("version") != 1
                or not isinstance(chain_state.get("erc8004"), dict)):
            parser.error("Unsupported checkpoint schema")
    if any((args.retry_in, args.retry_out, args.health_output)) and not all(
            (args.retry_in, args.retry_out, args.health_output)):
        parser.error("--retry-in, --retry-out and --health-output must be provided together")
    if args.query and args.retry_in:
        parser.error("Public retry state is only updated by scheduled indexing")
    retry_state, health = None, None
    if args.retry_in:
        try:
            retry_state = json.loads(Path(args.retry_in).read_text(encoding="utf-8"))
            crawl_retry.validate_retry_state(retry_state)
        except (ValueError, OSError, crawl_retry.RetryStateError):
            parser.error("Invalid public retry state")
        health = {}
    data = discover(args.query.strip(), config, token=os.environ.get("GITHUB_TOKEN", ""),
                    chain_state=chain_state, retry_state=retry_state,
                    health_report=health)
    if args.checkpoint_out:
        output_state = Path(args.checkpoint_out)
        output_state.parent.mkdir(parents=True, exist_ok=True)
        output_state.write_text(json.dumps(chain_state, sort_keys=True, indent=2) + "\n",
                                encoding="utf-8")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if retry_state is not None:
        staged = Path(args.retry_out)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(crawl_retry.validate_retry_state(retry_state))
        report = Path(args.health_output)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(health, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    print(f"Found {data['count']} Agent Cards, {len(data['warnings'])} warnings")


if __name__ == "__main__":
    main()
