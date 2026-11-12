# SearchA2A API

SearchA2A exposes public, read-only metadata about discovered A2A agents. It does not execute agent calls.

## Origins

- Search website and latest **crawl snapshot**: `https://searcha2a.azphalt.store`
- Persistent registry and search API: `https://searcha2a-api.hereliesaz.workers.dev`
- Azphalt's independent directory proxy: `https://azphalt.store/api/a2a/agents`

Public registry GET endpoints return `Access-Control-Allow-Origin: *`. No Turnstile or GitHub token is needed to read them.

## Persistent registry

### `GET /api/v1/agents?query=weather&limit=50&offset=0`

Returns matching stored Agent Cards synchronously. `query` is optional (empty lists all); maximum length is 120 characters. `limit` defaults to 50, maximum 100. `offset` defaults to 0, maximum 100000. Search is case-insensitive keyword matching across names, skills, descriptions, and advertised endpoint text, ranked by name then skills then description. This is **not** semantic inference or a live crawl.

Response shape:

```json
{
  "query": "weather",
  "count": 1,
  "total": 61,
  "offset": 0,
  "limit": 50,
  "generated_at": "2026-11-12T12:00:00Z",
  "results": [],
  "verified": "Agent Card metadata only; endpoints are not actively probed"
}
```

The counts and timestamp above are illustrative. `count` is the total number of query matches before pagination; `total` is the registry-wide record count. `results` contains the requested page of normalized Agent Cards (empty in this illustrative response). Fields include a 20-character hexadecimal `id`, `name`, `description`, `source_url`, `endpoint`, `skills`, `protocols`, `capabilities`, `security_schemes`, `version`, and `verification: "card_read_only"`. An unknown agent returns no match, not a live discovery job.

### `GET /api/v1/agents/{id}`

Read one persisted normalized Agent Card. Returns 404 if the ID is unknown.

### `GET /api/v1/directory?q=weather`

The existing semantic capability discovery endpoint remains available to clients such as Aive. It searches the **published static** `agent-feed` snapshot, not the accumulated SQL registry. It uses Workers AI embeddings when available and otherwise reports `match_mode: "keyword-fallback"`; its result cap is 20. Requires `q` of 2–120 characters and uses the existing approximate five-requests-per-minute IP limiter. It never dispatches GitHub Actions or invokes an agent. For all stored agents, including those absent from the latest snapshot, use `GET /api/v1/agents`.

Errors for public endpoints: 400 invalid input, 404 unknown agent, 503 registry binding unavailable, 500 unexpected failure. GET responses do not allocate per-request result mailboxes.

## Scheduled ingest (private)

### `POST /internal/registry/ingest`

Called only by `src/registry_sync.py` during the scheduled/push-triggered Pages indexing workflow. Requires `Authorization: Bearer <RESULT_CALLBACK_SECRET>`, the existing shared Actions/Worker secret. Until the secret is renamed, **it also protects the legacy callback route**. Do not expose this value to browsers or commits.

The request is a JSON discovery catalog with `query: ""` and up to 70 normalized `results`. Payloads over 1,000,000 characters are rejected. Each row must contain a valid card ID, name, public HTTPS card source, skills, and `verification: "card_read_only"`. The entire batch is validated before an atomic SQLite upsert. Existing entries absent from a crawl are retained. A successful response is `{"status":"stored","ingested":N,"total":M}`; errors include 400 invalid data, 401 missing secret, 413 excessive size, and 503 unconfigured registry.

No public write or agent execution endpoint is enabled.

## Registry review and lifecycle (private)

### `POST /internal/registry/manage`

Requires `Authorization: Bearer <REGISTRY_ADMIN_SECRET>`, which must be **different from** the crawler's `RESULT_CALLBACK_SECRET`. The gateway returns 401 if the administrator secret has not been configured. This separation prevents an indexing job from approving its own discoveries.

Send a 20-character lowercase agent `id`, an `action`, and a non-empty `reason` (up to 240 characters):

```json
{"id":"aaaaaaaaaaaaaaaaaaaa","action":"deactivate","reason":"Publisher requested removal"}
```

Supported actions: `approve`, `reject`, `reset_review`, `hide`, `deactivate`, and `activate`. **Approval means metadata review only**, not identity proof, endpoint testing, or an Azphalt signature. Rejected entries remain excluded even after `activate` until their review is reset or approved.

A 200 response includes `id`, `listing_state`, `review_status`, and `changed_at`. Invalid requests return 400; unknown agents 404; excess body size 413. Each decision and its reason are stored in a durable private audit trail.

### `GET /internal/registry/audit/{id}`

Requires the same administrator secret. Returns the current listing and review states plus the 100 most recent state changes, newest first. Audit records are private and independent of crawler metadata.

**Public visibility:** `GET /api/v1/agents` and `GET /api/v1/agents/{id}` include only active, non-rejected entries. Visible Agent Cards include a `curation_status` of `unreviewed` or `approved`; the underlying `verification` remains `card_read_only`. Reingesting a card updates its metadata but preserves curator-controlled states. The static Pages snapshot and Azphalt feed are filtered against the registry's suppression manifest on the next scheduled, push-triggered, or manual index publication. An administrator decision takes effect immediately on the live registry, and on the static feeds after publication. A crawler sync fails closed if suppression information is missing.

## Static GitHub Pages endpoints

- `GET /api/v1/catalog.json`: latest bounded crawl snapshot, **not** the full persistent registry.
- `GET /api/v1/agents/{id}.json`: per-agent records from that snapshot.
- `GET /api/v1/azphalt-listings.json`: read-only, non-installable Azphalt export from that snapshot, mirrored to the `agent-feed` Git branch.

These static endpoints are periodically rebuilt; missing cards in a snapshot may still exist in the persistent registry.

## Retired on-demand search

`POST /api/v1/search` returns **410 Gone** and never dispatches an Action. Existing `GET /api/v1/results/{uuid}` and authenticated `POST /internal/results` are temporarily retained to finish/search historical requests, subject to their seven-day mailbox expiry. New searches never create those mailboxes.

All agent metadata is unverified third-party content, not proof that an endpoint is currently operational or safe.
