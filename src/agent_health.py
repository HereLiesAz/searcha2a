#!/usr/bin/env python3
"""Bounded, scheduled HTTPS reachability checks. Never execute an A2A task."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


def probe(item: dict) -> dict:
    """A single credential-free HEAD, no redirects, private hosts or response bodies."""
    identifier, url = item["id"], item["endpoint"]
    result = {"id": identifier, "endpoint": url, "status": "skipped", "http_status": None}
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
            connection.request("HEAD", parsed.path or "/" , headers={
                "Host": parsed.hostname, "User-Agent": USER_AGENT, "Accept": "*/*",
            })
            code = connection.getresponse().status
            result.update(status="responding" if code < 500 else "server_error",
                          http_status=code)
        finally:
            connection.close()
    except DiscoveryError:
        return result
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
        result["status"] = "unreachable"
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
