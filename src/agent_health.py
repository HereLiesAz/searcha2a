#!/usr/bin/env python3
"""Bounded, scheduled HTTPS reachability checks. Never execute an A2A task."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from discover import DiscoveryError, require_public_https, USER_AGENT, NoRedirect

MAX_CHECKS = 60
TIMEOUT_SECONDS = 5

# A credential-free HEAD can only show that a server answered at the address.
# Codes that mean "a server is here and wants a POST or a key" count as reachable.
# 404/410 mean nothing is served at that path; other codes are not interpreted.
REACHABLE_CODES = frozenset({200, 201, 202, 204, 400, 401, 402, 403, 405, 415, 429})
# `status` is the legacy field (kept for existing readers); `state` is the honest taxonomy.
LEGACY_STATUS = {"reachable": "responding", "server_error": "server_error",
                 "path_not_found": "path_not_found", "redirected": "redirected",
                 "unexpected_status": "unexpected_status", "unreachable": "unreachable",
                 "tls_error": "unreachable", "skipped": "skipped"}


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect only to a previously validated public IP while verifying the host TLS cert."""

    def __init__(self, hostname: str, address: str):
        super().__init__(hostname, port=443, timeout=TIMEOUT_SECONDS,
                         context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def valid_redirect_target(location) -> bool:
    """A redirect only counts when it names an ordinary public https hostname. Never followed."""
    if not isinstance(location, str) or not location or len(location) > 1000:
        return False
    try:
        parsed = urllib.parse.urlsplit(location)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 443):
            return False
        try:
            ipaddress.ip_address(host)
            return False  # IP literals are refused outright
        except ValueError:
            return "." in host and not host.endswith((".local", ".internal", ".localhost"))
    except ValueError:
        return False


def classify(code: int, location=None) -> tuple[str, bool]:
    """Return (state, redirect_flag) for a HEAD response code."""
    if 300 <= code < 400:
        return ("reachable", True) if valid_redirect_target(location) else ("redirected", True)
    if code >= 500:
        return "server_error", False
    if code in (404, 410):
        return "path_not_found", False
    if code in REACHABLE_CODES:
        return "reachable", False
    return "unexpected_status", False


def challenge_kind(value) -> str | None:
    """Only the authentication scheme word survives; realms and parameters are discarded."""
    if not isinstance(value, str) or not value.strip():
        return None
    word = value.strip().split(" ", 1)[0].lower()
    return word if word in ("bearer", "basic", "digest", "negotiate") else "other"


def _tls_not_after(connection) -> str | None:
    try:
        certificate = connection.sock.getpeercert()
        expires = ssl.cert_time_to_seconds(certificate["notAfter"])
        return datetime.fromtimestamp(expires, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def probe(item: dict) -> dict:
    """A single credential-free HEAD, no redirects, private hosts or response bodies."""
    identifier, url = item["id"], item["endpoint"]
    result = {"id": identifier, "endpoint": url, "status": "skipped", "state": "skipped",
              "http_status": None}
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.query or parsed.fragment:
            return result  # URLs bearing tokens must never be sent as health probes.
        require_public_https(url)
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            return result
        # DNS can change; pin the already-checked address rather than resolving at connect.
        connection = PinnedHTTPSConnection(parsed.hostname, addresses[0][4][0])
        try:
            connection.request("HEAD", parsed.path or "/", headers={
                "Host": parsed.hostname, "User-Agent": USER_AGENT, "Accept": "*/*",
            })
            response = connection.getresponse()
            code = response.status
            header = getattr(response, "getheader", lambda *_: None)
            state, redirect = classify(code, header("Location"))
            result.update(state=state, status=LEGACY_STATUS[state], http_status=code,
                          redirect=redirect)
            if code in (401, 402, 403):
                result["challenge"] = challenge_kind(header("WWW-Authenticate")) or "none"
            expiry = _tls_not_after(connection)
            if expiry:
                result["tls_not_after"] = expiry
        finally:
            connection.close()
    except DiscoveryError:
        return result
    except ssl.SSLError:
        result.update(status="unreachable", state="tls_error")
    except (OSError, http.client.HTTPException, ValueError):
        result.update(status="unreachable", state="unreachable")
    return result


def api_request(base: str, secret: str, path: str, payload: dict | None = None,
                opener=None) -> dict:
    """Only the configured registry receives the crawler credential."""
    if not base.startswith("https://") or not secret:
        raise ValueError("Health sync requires a configured HTTPS registry and credential")
    opener = opener or urllib.request.build_opener(
        NoRedirect(), urllib.request.ProxyHandler({})).open
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base.rstrip("/") + path, data=data,
        headers={"Authorization": "Bearer " + secret,
                 "Content-Type": "application/json",
                 "User-Agent": "SearchA2A-Health"},
        method="GET" if data is None else "POST")
    with opener(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError("Health registry unavailable")
        return json.load(response)


def run(base: str, secret: str, *, opener=None, probe_fn=probe) -> dict:
    due = api_request(base, secret, "/internal/registry/health/due", opener=opener)
    entries = due.get("entries")
    if (not isinstance(entries, list) or len(entries) > MAX_CHECKS or
            any(not isinstance(entry, dict) or
                not isinstance(entry.get("id"), str) or
                not isinstance(entry.get("endpoint"), str) for entry in entries)):
        raise ValueError("Invalid health due list")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe_fn, entries))
    uploaded = api_request(base, secret, "/internal/registry/health",
                           {"results": results}, opener=opener)
    if uploaded.get("status") != "stored" or not isinstance(uploaded.get("stored"), int):
        raise RuntimeError("Health registry did not acknowledge results")
    return {"attempted": len(results), "stored": uploaded["stored"]}


def main() -> None:
    result = run(os.environ.get("REGISTRY_API_URL", ""),
                 os.environ.get("REGISTRY_SYNC_SECRET", ""))
    print(f"Endpoint HEAD checks: {result['attempted']} attempted, "
          f"{result['stored']} stored (reachability only, no A2A tasks)")


if __name__ == "__main__":
    main()
