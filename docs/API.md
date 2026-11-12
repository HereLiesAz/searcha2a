# SearchA2A API reference

This is the implemented HTTP interface for **agent discovery metadata**, not an API for calling or running remote agents. The source of truth is `worker/index.mjs`, `src/discover.py`, `src/azphalt_listings.py` and `.github/workflows/pages.yml`.

## Base URLs

| Service | Base URL | Access |
| --- | --- | --- |
| Public catalog and site | `https://searcha2a.azphalt.store` | No credentials for JSON files |
| Live search gateway | `https://searcha2a-api.hereliesaz.workers.dev` | Turnstile-verified browser search |
| Azphalt directory proxy | `https://azphalt.store` | Public read-only feed |

The deployed site's `/config.json` supplies the live API base URL. It is a public client configuration file; it does not contain the private Turnstile secret or GitHub token.

## Static JSON API (GitHub Pages)

### `GET /api/v1/catalog.json`

The latest scheduled/published discovery snapshot. Fields: `query` (string; empty for the scheduled catalog), `generated_at` (ISO timestamp or null), `count` (integer), `results` (array), `warnings` (array of messages), and `verified` (string explaining metadata-only verification).

Catalog generation returns up to 70 agent records and ten warnings. A record has a 20-character lowercase hexadecimal `id`, `name`, `description`, `endpoint` (an optional advertised HTTPS URL), `source_url`, `source`, `skills`, `protocols`, `capabilities`, `security_schemes`, `version` and `verification: "card_read_only"`. Skills contain `name`, `description` and `tags`. Security schemes are names, **not credentials**.

The catalog is periodically rebuilt. It is not a real-time agent status endpoint.

### `GET /api/v1/agents/{id}.json`

A published record for a known card ID, generated from the current catalog. An unknown ID does not trigger discovery; GitHub Pages returns its ordinary not-found response.

### `GET /api/v1/azphalt-listings.json`

A read-only directory export. A response has `schema: 1`, `source: "searcha2a"`, `generated_at`, `count` and `listings`. Each listing is an external card with `id` prefixed by `external.a2a.`, `kind: "a2a-agent"`, `verification: "card_read_only"` and `installable: false`, plus its descriptive fields.

The generated feed is mirrored to the `agent-feed` Git branch at `site/api/v1/azphalt-listings.json` for the Azphalt Store proxy. The Store exposes `GET https://azphalt.store/api/a2a/agents`; this is an **Azphalt Store route**, not a route on the SearchA2A gateway. Azphalt's UI is at [azphalt.store/agents](https://azphalt.store/agents). Do not treat these listings as signed `.azp` packages or trusted executables.

## Live search (Cloudflare Worker)

### `POST /api/v1/search`

Requires a JSON body and an exact `Origin: https://searcha2a.azphalt.store` header. The browser obtains a single-use Cloudflare Turnstile response token from the site.

```http
POST /api/v1/search
Origin: https://searcha2a.azphalt.store
Content-Type: application/json

{"query":"weather","turnstile":"<Cloudflare Turnstile response token>"}
```

The Worker trims/collapses whitespace, validates a query of **2–120 characters**, verifies Turnstile and applies an approximate rate limit of **five searches per client IP per 60 seconds**. It creates a random UUID mailbox and dispatches a GitHub Actions run. The search runs in Actions, not inside the HTTP request.

**202 Accepted** (fields returned by the Worker):

```json
{
  "id": "aab1cc22-1111-4111-8111-123456789abc",
  "query": "weather",
  "status": "queued",
  "workflow_run_id": null,
  "workflow_url": null
}
```

The example UUID is illustrative. GitHub may return workflow metadata; both workflow fields can be null.

Possible failures: `400` invalid JSON/query, `403` origin or Turnstile failure, `429` rate limit, `502` GitHub dispatch unreachable/rejected, `503` gateway or mailbox unavailable, `500` unexpected error. Error bodies are JSON objects with an `error` string.

**This endpoint is not a freely callable service API.** An arbitrary script with no valid Turnstile token cannot dispatch searches, and an `Origin` header alone is not authentication.

### `GET /api/v1/results/{uuid}`

No Turnstile token is needed to read an existing request ID. The UUID acts as an unguessable locator, not as a per-user login. Do not publish search IDs or submit sensitive queries.

**200 OK** while waiting:

```json
{
  "id": "aab1cc22-1111-4111-8111-123456789abc",
  "query": "weather",
  "status": "queued",
  "workflow_run_id": null,
  "workflow_url": null
}
```

The initial queued record may lack the workflow metadata keys until dispatch returns.

**200 OK** on completion:

```json
{
  "id": "aab1cc22-1111-4111-8111-123456789abc",
  "query": "weather",
  "status": "complete",
  "results": [],
  "count": 0,
  "warnings": [],
  "generated_at": "2026-11-12T10:22:30Z"
}
```

Empty results are valid. Results are limited to 70 cards; warnings to ten messages. `workflow_run_id` and `workflow_url` may also appear in a complete response if dispatch metadata arrived before the callback.

**400** denotes malformed UUID; **404** denotes an unknown, expired or deleted mailbox; **500** denotes unexpected Worker failure. Completed results are retained for up to **seven days** from request creation, then removed by a Durable Object alarm. The same Durable Object serves callback updates and GET requests, so there is no Workers KV cross-region propagation delay.

The public page polls every **3 seconds**, at most **48 times** (approximately 144 seconds excluding network time). It does not provide a streaming API.

### `OPTIONS`

An OPTIONS request whose `Origin` exactly matches the configured website receives **204** and CORS headers for `GET, POST, OPTIONS` and `Content-Type`. A disallowed origin receives **403**.

## Private callback

### `POST /internal/results`

Used by `src/report.py` in the GitHub Actions workflow. **Not a public write API.** Requires:

```http
POST /internal/results
Authorization: Bearer <RESULT_CALLBACK_SECRET>
Content-Type: application/json

{"request_id":"aab1cc22-1111-4111-8111-123456789abc","query":"weather","results":[],"warnings":[],"generated_at":"2026-11-12T10:22:30Z"}
```

The callback can complete only a still-queued record whose UUID and query match. A second completion cannot replace the first. Accepted response is **200** with `{"status":"stored"}`. Failures: **401** missing/incorrect secret, **400** invalid JSON/UUID, **413** callback body exceeding the configured 700,000-character guard, **404** unknown/expired request, **409** mismatched query/ID, wrong result type or already-complete request.

## Data and verification limits

SearchA2A fetches public Agent Cards over HTTPS with public-DNS checks, redirects disabled, a seven-second per-source timeout, a 512 KiB JSON-document ceiling, and at most 90 card candidates. Discovery uses up to eight concurrent card fetches. Search is bounded and incomplete by design: GitHub API search availability, rate limits, source failures and registry formats all affect coverage.

A published card is **metadata read only**, not proof of identity, runtime availability, or A2A conformance.
