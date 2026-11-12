# SearchA2A architecture

[User guide](../README.md) · [API reference](API.md) · [Operations](OPERATIONS.md)

SearchA2A separates **static discovery**, **live execution**, **authenticated dispatch**, and **strongly consistent results**. It searches public agent metadata, not running agent services.

## Data flow

```text
Browser (GitHub Pages + Turnstile)
    -> Worker (origin, Turnstile, query, rate checks)
    -> SearchResult Durable Object (queued mailbox, UUID)
    -> GitHub workflow_dispatch (search.yml)
    -> discover.py (curated Agent Cards + registries + GitHub search)
    -> report.py (authenticated callback)
    -> Worker -> same Durable Object (completed result)
    -> GET /api/v1/results/{uuid} -> browser
```

The public catalog is a separate scheduled build:

```text
pages.yml -> discover.py (empty query)
  -> /api/v1/catalog.json
  -> /api/v1/agents/{id}.json
  -> azphalt_listings.py -> /api/v1/azphalt-listings.json
                         -> Git agent-feed branch (raw.githubusercontent.com)
                         -> Azphalt Store /api/a2a/agents and /agents
```

The live search does not rewrite the static catalog. The Azphalt Store feed is a non-installable metadata export, not an agent registry that executes code.

## Decisions and reasons

- **GitHub Actions for live discovery.** External-source discovery is bounded and uses GitHub search permissions; keeping it in Actions avoids leaking a write-authorized dispatch token to the browser. The Worker only authorizes requests and dispatches jobs.
- **SQLite-backed Durable Objects rather than Workers KV for results.** Cross-region KV reads could lag behind callback writes. Mapping each random UUID to one Durable Object ensures the callback and poller access the same strongly consistent mailbox. It does not remove GitHub Actions startup or network latency.
- **Static GitHub Pages plus separate Worker origin.** Pages hosts read-only files and cannot safely store GitHub Actions-write or Turnstile secrets. The gateway accepts only the configured browser origin for dispatch.
- **An external-only Azphalt feed.** Discovery proves only that card metadata was readable. Azphalt must not confuse an unverified card with a signed `.azp` package, model or trusted, installed agent.
- **Raw GitHub mirror for the store feed.** An Azphalt Cloudflare Worker subrequest to `searcha2a.azphalt.store` on the same Cloudflare zone previously returned 502. The feed is mirrored to `agent-feed` and fetched from `raw.githubusercontent.com` instead.

## Implementation map

| Concern | Implementation |
| --- | --- |
| Browser UI, challenge rendering, poller | `site/app.js` and `site/index.html` |
| Public JSON and mirror publication | `.github/workflows/pages.yml` |
| Cloudflare gateway and mailbox | `worker/index.mjs`; `worker/wrangler.toml` |
| Gateway deployment and smoke test | `.github/workflows/gateway.yml` |
| On-demand discovery and callback | `.github/workflows/search.yml`; `src/discover.py`; `src/report.py` |
| Normalized store listings | `src/azphalt_listings.py` |
| Test suites | `tests/`; `.github/workflows/ci.yml` |

## Mailbox lifecycle

`SearchResult` is a Durable Object class with an SQLite-backed storage binding. Its `ctx.storage.kv` API is **Durable Object synchronous storage**, not Workers KV. A random UUID selects an object via `RESULTS.idFromName(id)`. On creation, the object records a `queued` request and schedules a seven-day alarm. On callback it accepts only the matching request ID and query while still queued, writes bounded `complete` results, and rejects duplicate completion. Dispatch metadata updates cannot overwrite a completed callback. A failed GitHub dispatch deletes the pending mailbox. Reads of unknown or expired IDs return 404; the alarm removes stored data.

The initial Durable Object deployment used the `v1` migration in `worker/wrangler.toml`. Previous Workers KV requests were intentionally **not** migrated, because the old mailbox was eventually consistent and the cutover introduced a new storage identity. A request started on the old Worker must be resubmitted; the old KV namespace is no longer bound.

## Trust boundaries and enforcement

| Invariant | Enforced by |
| --- | --- |
| Exact browser origin check, valid Turnstile token and approximate per-IP rate limit before dispatch | `worker/index.mjs`: `handle` (`POST /api/v1/search`), `verifyTurnstile` |
| Dispatch secret stays server-side | `worker/index.mjs`: private GitHub Authorization header; `worker` secret configuration |
| Callback requires separate secret | `worker/index.mjs`: `POST /internal/results` |
| Callback matches the still-queued UUID and query; duplicate completion refused | `SearchResult.fetch` (`/complete`) |
| Expiration after seven days | `SearchResult.fetch` expiry guard; `SearchResult.alarm` |
| Maximum of 70 completed result cards | `SearchResult.fetch` (`/complete`) |
| External listings cannot advertise themselves as installable | `src/azphalt_listings.py`: `build_listings` |
| Discovery inputs are bounded and restricted to public HTTPS | `src/discover.py`: `require_public_https`, `fetch_json`, `discover` |

## Limits

- `src/discover.py` fetches up to 90 card candidates, up to 524,288 bytes per JSON document, with a seven-second timeout and eight concurrent card readers. It returns at most 70 cards and ten warnings.
- Its HTTPS/DNS checks apply to URLs it **fetches**, not to advertised agent endpoints. It disables redirects and rejects local/non-public IPs, but DNS can change between validation and connection.
- An Agent Card is unverified descriptive data. It cannot prove identity, availability, supported protocols at runtime or authorization to act on the user's behalf.
- GitHub search indexing and permissions, unavailable registries, malformed cards and network errors can reduce coverage. Results may legitimately be empty.
- The Worker requires a valid browser Turnstile response and restricts the origin, but `Origin` alone is not authentication. Turnstile and the approximate rate limiter do not cap total Actions spend.
- Search IDs are opaque locators, not user login tokens. Anyone who knows a valid UUID can read its result until expiry. Do not search for private information.

## Verification boundary

The gateway deployment workflow tests **404 from an unknown UUID**, which proves the mailbox binding responds. It does **not** test an authenticated dispatch, external GitHub Actions run or callback. Full integration verification requires a real browser Turnstile challenge, an Actions run, a successful callback and a subsequent `status: "complete"` result. See [Operations](OPERATIONS.md).
