"""Optional, automatic verification of external A2A publisher-controlled hosts.

An Agent Card is untrusted even when served over HTTPS. Promotion needs an
explicit opt-in IN the card and a fresh, matching proof hosted under the same
HTTPS origin. This only establishes control of that host and consistency of
advertised protocol metadata. It is NOT an Azphalt package signature, user
permission, service reliability or legal identity check.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import ipaddress


PROOF_PATH = "/.well-known/azphalt-a2a-verification.json"
MAX_PROOF_AGE = timedelta(days=7)
MAX_ATTESTATION_TTL = timedelta(days=1)


def _utc_date(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return date.astimezone(timezone.utc) if date.tzinfo is not None else None


def _public_origin(value: str):
    """Require a plain HTTPS origin with no alternate port, credentials or fragments."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443):
            return None
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host:
                return None
        return "https://" + parsed.hostname.lower()
    except ValueError:
        return None


def verify_publisher(record: dict, card: dict, *, fetch, now: datetime | None = None):
    """Return bounded, independently checked evidence, else None (fail closed).

    Publishers opt in by advertising azphalt.externalAgentListing == true and
    by publishing a fresh exact-URL statement at their own origin. The fetch
    callback MUST use the crawler's public-DNS / HTTPS / no-redirect reader.
    """
    if not isinstance(card, dict) or card.get("azphalt") != {"externalAgentListing": True}:
        return None
    if record.get("verification") != "card_read_only":
        return None
    card_url, endpoint = record.get("source_url"), record.get("endpoint")
    origin = _public_origin(card_url)
    if not origin or origin != _public_origin(endpoint):
        return None
    # Source URL and A2A endpoint must be same origin; a card cannot opt another
    # service into a supposedly host-verified Store entry.
    binding = card.get("supportedInterfaces")
    json_rpc = (
        isinstance(binding, list) and
        any(isinstance(item, dict) and item.get("protocolBinding") == "JSONRPC" and
            item.get("url") == endpoint for item in binding)
    )
    if not json_rpc:
        return None
    proof_url = origin + PROOF_PATH
    try:
        proof = fetch(proof_url)
    except Exception:  # Public proof can disappear, exceed limits, or become malformed.
        return None
    if not isinstance(proof, dict) or proof.get("schema") != 1 or not (
        proof.get("listing") == "azphalt-external-a2a"
        and proof.get("consent") is True
        and proof.get("card_url") == card_url
        and proof.get("endpoint") == endpoint
    ):
        return None

    checked = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        raise ValueError("Verification clock must have a timezone")
    checked = checked.astimezone(timezone.utc)
    issued = _utc_date(proof.get("issued_at"))
    expires = _utc_date(proof.get("expires_at"))
    if not issued or not expires or issued > checked + timedelta(minutes=5):
        return None
    if checked - issued > MAX_PROOF_AGE or not (checked < expires <= issued + MAX_PROOF_AGE):
        return None
    valid_until = min(expires, checked + MAX_ATTESTATION_TTL)
    return {
        "method": "https-origin-opt-in",
        "proof_url": proof_url,
        "checked_at": checked.isoformat(),
        "expires_at": valid_until.isoformat(),
        "host": urlsplit(origin).hostname,
    }
