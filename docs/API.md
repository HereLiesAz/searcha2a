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

The counts and timestamp above are illustrative. `count` is the total number of query matches before pagination; `total` is the registry-wide record count. `results` contains the requested page of normalized Agent Cards (empty in this illustrative response). Fields include a 20-character hexadecimal `id`, `name`, `description`, `source_url`, `endpoint`, `skills`, `protocols`, `capabilities`, `security_schemes`, `version`, `schema_status`, `raw_sha256`, optional `raw_card` (public JSON capped at 4 KiB), and `verification: "card_read_only"`. Partial v1 cards with an explicit HTTPS supported interface may legitimately have an empty `skills` list. Raw Agent Card metadata is untrusted; JWS verification is not implied. Public directory search records retain `verification: "card_read_only"`; no directory's advertised health checks or ranking are adopted as SearchA2A task-conformance proof. Authorized sources with explicit public-publication permission are labeled `source: "authorized-catalog"`; their raw card objects are not exported, but normalized metadata and source URLs are publicly visible. An unknown agent returns no match, not a live discovery job.

### `GET /api/v1/agents/{id}`

Read one persisted normalized Agent Card. Returns 404 if the ID is unknown.

### `GET /api/v1/agents/{id}/health`

Returns the latest scheduled *HTTPS endpoint reachability* observation for a visible agent, or 404 for an unknown or moderated-out agent. Shape: `{"id":"...","status":"responding","state":"reachable","http_status":405,"checked_at":"...","protocol_verified":false,"redirect":false}`, plus `challenge` (`bearer`, `basic`, ... only the scheme word of a 401/402/403 answer) and `tls_not_after` when known. `state` is the honest taxonomy: `reachable`, `path_not_found` (HTTP 404/410), `redirected` (a 3xx with no usable target), `server_error`, `unexpected_status`, `unreachable`, `skipped`. `status` is the legacy field kept for existing readers: `unchecked`, `responding` (only for `state: reachable`), `server_error`, `unreachable`, `skipped`, `stale` (older than seven days), and, **new and additive**, `path_not_found`, `redirected`, `unexpected_status`. Earlier versions reported every code below 500 as `responding`; HTTP 404 is no longer called that, and rows stored before this change are reclassified when read. A 3xx counts as `reachable` only when its `Location` is an ordinary public `https` hostname (it is never followed) and `redirect` is then `true`. Codes 200-204, 400-403, 405, 415 and 429 count as `reachable` because a server that wants a POST or a key answers a `HEAD` that way. Unchecked entries have a null `checked_at`. A response to `HEAD`, even HTTP 200, does **not** demonstrate A2A protocol conformance, authorization, identity or task availability. Public GET requests never trigger endpoint probes.

### `GET /api/v1/agents/{id}/verification`

Returns the evidence behind a listing's level, computed when you ask (see [What verification levels mean](#what-verification-levels-mean)). 404 for an unknown or moderated-out agent. Public, read-only, CORS `*`.

```json
{
  "id": "9080ee8eb995e09faa25", "level": 2, "level_name": "endpoint_reachable", "reviewed": false,
  "computed_at": "2026-11-13T03:40:00Z",
  "checks": {
    "card": {"state": "ok", "checked_at": "...", "expires_at": "...", "card_sha256": "...", "source_url": "..."},
    "origin": {"state": "match", "card_origin": "https://a.example", "endpoint_origin": "https://a.example",
               "shared_platform": null, "consent_proof": false},
    "endpoint": {"state": "reachable", "method": "HEAD", "http_status": 405, "redirect": false,
                 "checked_at": "...", "expires_at": "...", "probe_version": 2},
    "identity": {"state": "none"}
  },
  "summary": "Checked 1 day ago: a server answered at its address. That does not mean it can do your job.",
  "caveat": "No one has tested what this agent does.",
  "not_checked": ["accepts_tasks", "speaks_a2a", "correct_output", "safe", "legal_identity", "pricing_accuracy"]
}
```

States: `card`: `ok` or `expired`. `origin`: `match`, `mismatch`, `unknown`, `skipped`. `endpoint`: `unchecked`, `expired`, or an endpoint state from the health endpoint. There is no per-check history yet.

### `GET /api/v1/providers?kind=declared_provider&min_agents=1&limit=50&offset=0`

Groups visible agents by what publishers declared: `declared_provider` (the Source a card names), `hosting` (the Host, inferred from the endpoint's website name) or `model_provider` (only from the structured `https://searcha2a.azphalt.store/ext/backing/v1` card extension). Each group has `name`, `agents`, `keyless_n`, `level_histogram`, `basis`, `confidence` (`declared` or `inferred_from_host`). Names are publisher text and are unconfirmed. `limit` at most 50, `offset` at most 200.

### Additive fields on agent reads (schema revision 1.1)

`GET /api/v1/agents`, `/agents/{id}` keep every existing field and add:

- `verification_detail`: `{level, level_name, reviewed, computed_at, checked_at, expires_at, summary}` computed at read time. The legacy `verification` stays `"card_read_only"`.
- `access`: what the card declares about keys and payment: `auth_declared`, `auth_types` (from the scheme **type**, not the publisher's key name), `keyless` (`declared`, `no`, `unknown`; never `observed`), `payment_required` (`declared`, `none_declared`, `unknown`), `observed_challenge` (`"401"`, `"402"`, `"403"` or `null`, from a fresh probe), `flags`, `evidence`. `declared` means the card is silent about keys; it is not a promise that none is needed or that the agent is free. `unknown` is used for records stored before this field existed, and when a card has a security requirement without any scheme.
- `backing`: `declared_provider` (`organization`, `url`, `url_matches_endpoint`), `hosting` (`platform`, `shared_platform`, `host`), `model_provider` (only from the structured extension), `confidence`, `evidence`, `unknowns`. Derived only from the card and the endpoint's website name. Nothing is guessed from descriptions.
- `provider`: the card's own `provider` `{organization, url}`, kept even when the 4 KiB `raw_card` is omitted.

Optional filters on `GET /api/v1/agents` (all validated; invalid values return 400): `keyless` (`declared`, `no`, `unknown`, `any`), `payment` (`declared`, `none_declared`, `unknown`, `any`), `min_level` (0-3), `reviewed` (`true`/`false`), `provider` (substring of the declared organization), `hosting` (platform label), `protocol` (`JSONRPC`, `HTTP+JSON`, `GRPC`), `skill` (substring of skill text), `fresh_within_days` (1-30), `sort` (`relevance` default, `level`, `keyless_first`, `recently_checked`). Because level, keyless and freshness are computed at read time, a request using them scans at most 2,000 stored cards and then pages, so `count` is the filtered total. These filters never change relevance ordering unless `sort` says so.

Records ingested before the crawler change lack `backing`, `provider` and parts of `access` until their next crawl. They read as `keyless: "unknown"`.

## What verification levels mean

Levels are **not stored**. They are recomputed on every read from evidence rows that expire after 7 days and that are bound to the card's current `endpoint` and `raw_sha256`. A level never rises because of something the card says about itself (`verified`, `x-status`, `trustModels`, a long description, a signature object): card text is data only. Missing, expired or failed evidence lowers the level.

| Level | `level_name` | Needs | Does not show |
| --- | --- | --- | --- |
| 0 | `card_read` | The card was read from a public HTTPS address in the last 7 days | Anything about the endpoint or who runs it |
| 1 | `origin_consistent` | Level 0, and the card's address and the agent's endpoint have the same scheme, host and port | Who runs that website. On shared hosts (workers.dev, vercel.app, ...) it shows only the same account subdomain |
| 2 | `endpoint_reachable` | Level 1, and a credential-free `HEAD` in the last 7 days found a server at the exact endpoint (state `reachable`), on a certificate with at least 7 days left | That it speaks A2A, accepts tasks, is authorized, is correct, or is free |
| 3 | `domain_control_proven` | **Not implemented.** Would need a DNSSEC-validated DNS record or a signed card checked against a key on the same website | Reserved. `identity.state` is always `none`, so no listing reaches level 3 |

`reviewed` is a separate flag, not a level: an administrator approved the listing (metadata review only, never an endpoint test) and the approval is under 180 days old. An older approval reads as `reviewed: false` with `review_expired: true`; the card stays visible.

Authorized private-catalog records stay at level 0 (they are never probed).

The fixed sentences shown to people are in `worker/verification.mjs` (`WORDING`). They are never assembled from card text and never use the words "verified", "safe", "trusted" or "official". Examples: "Listed only. Nobody has checked this agent." / "Checked 3 days ago: a server answered at its address. That does not mean it can do your job." / "Last checked 12 days ago. It may be offline." / "A server answered, but not at the agent's address. It may not be working." Beneath any level: "No one has tested what this agent does." Apps that cannot parse `verification_detail` should show the level 0 wording.

### `GET /api/v1/directory?q=weather`

The existing semantic capability discovery endpoint remains available to clients such as Aive. It ranks all active, non-rejected candidates from the **persistent SQL registry** (paged in batches of 100, newest indexed first) with persistent, incrementally updated Workers AI embeddings when available and otherwise reports `match_mode: "keyword-fallback"`. Once candidate vectors are cached, only the query is embedded. Its result cap is 20. The response carries `candidate_scope: "persistent-registry"`, `candidate_count` (all eligible persisted records examined, distinct from the 20-result cap), and `truncated: false` after reading every eligible candidate. Requires `q` of 2–120 characters and uses the existing approximate five-requests-per-minute IP limiter. No remote Agent Cards, static feeds or GitHub Actions are contacted. A missing registry binding returns 503 instead of falling back to potentially suppressed static listings. For complete metadata keyword search, use `GET /api/v1/agents`.

Errors for public endpoints: 400 invalid input, 404 unknown agent, 503 registry binding unavailable, 500 unexpected failure. GET responses do not allocate per-request result mailboxes.

## Static crawler health snapshot

### `GET https://searcha2a.azphalt.store/api/v1/crawler-health.json`

Pages publishes aggregate public-source Agent Card fetch statistics from the latest **successful scheduled indexing run**, independently of the persistent registry API. Counters include `attempted`, `accepted`, `invalid`, `unavailable`, `pending`, `queue_full`, `due_selected`, `retry_deferred`, `generated_at`, and `by_source` categories. `accepted` means a card or manifest pointer was readable, **not** that an agent answered an A2A task. Only public source categories are counted. No individual URLs, private catalog names, RPC tokens, agent endpoints, or raw error messages are exposed. This is a publication-time health snapshot, not a live health probe or liveness guarantee.

## Scheduled ingest (private)

### `POST /internal/registry/ingest`

Called only by `src/registry_sync.py` during the scheduled/push-triggered Pages indexing workflow. Requires `Authorization: Bearer <RESULT_CALLBACK_SECRET>`, the existing shared Actions/Worker secret. Until the secret is renamed, **it also protects the legacy callback route**. Do not expose this value to browsers or commits.

The request is a JSON discovery catalog with `query: ""` and up to 70 normalized `results` per upload. The scheduled synchronizer splits crawls of up to 150 cards into uploads of 70 cards / 1,000,000 encoded bytes, combining suppression IDs across all responses. A single oversized card fails closed. Each row must contain a valid card ID, name, public HTTPS card source, a skills array (possibly empty only for marked partial A2A interface cards), and `verification: "card_read_only"`. Rows may also carry the additive `provider`, `backing` and `access` objects; if present they are bounded and shape-checked (unknown enums such as `keyless: "observed"` reject the whole batch). The entire batch is validated before an atomic SQLite upsert. Existing entries absent from a crawl are retained. A successful response is `{"status":"stored","ingested":N,"total":M,"suppressed_ids":[]}`; errors include 400 invalid data, 401 missing secret, 413 excessive size, and 503 unconfigured registry.

No public write or agent execution endpoint is enabled. Authenticated private catalog connectors only read a configured HTTPS JSON feed (or the explicitly allowlisted Google Cloud Agent Registry listing API with bounded pagination) with a scoped credential provided to the scheduled crawler; they never expose an authorization header to browsers, public registry APIs, or linked Agent Card requests. Publication requires explicit `publish: true` and is subject to normal registry moderation.

## Scheduled endpoint checks (private)

`GET /internal/registry/health/due` and `POST /internal/registry/health` require the scheduled crawler's existing `RESULT_CALLBACK_SECRET`. The former returns up to 60 oldest or unchecked *visible public* endpoint records. Authorized private-catalog endpoints are excluded. The latter accepts at most 60 bounded reports with `id`, `endpoint`, `status` and `http_status`, and optionally `state`, `redirect`, `challenge` and `tls_not_after` (the Worker recomputes the honest state from the HTTP code, so a report that calls a 404 "responding" is stored as `path_not_found`). The registry only stores reports whose ID and endpoint still match an active, non-rejected public record. Health snapshots are separate from Agent Card verification and curator decisions.

The scheduled job sends a credential-free HTTPS `HEAD` to the endpoint using pinned, publicly routable DNS addresses, strict TLS, a five-second timeout, and no redirects or task bodies. Query-bearing endpoint URLs are skipped to avoid leaking tokens. Failures never cause cards to be deactivated or remove persisted metadata.

### `GET /internal/registry/evidence-export`

Requires the crawler's `RESULT_CALLBACK_SECRET` (not the admin secret). Returns `{generated_at, entries:[{id, verification_detail, access:{keyless, observed_challenge}}]}` for visible agents, used by the Pages job to add `verification_detail` to the Azphalt feed. It fails soft: if it is unavailable the feed is published without `verification_detail`.

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

## Publisher-defined ANS names

SearchA2A accepts `ans://<version>.<agentHost>` with an opaque,
publisher-defined version. For example `ans://1.0.0.weather.example.com` or
`ans://release-2026.11.weather.example.com`. DNS labels must be valid,
but SearchA2A neither enforces SemVer nor adds or removes `v`.
The Agent Card must explicitly supply both `ansName` and `agentHost`:
dotted versions cannot safely be separated from a host by guessing.

### `GET /api/v1/ans/resolve?name=ans%3A%2F%2F1.0.0.weather.example.com`

Reads a name claim from the *persistent curated registry*. It performs no
DNS, certificate, transparency-log, or live endpoint requests. On a match,
returns `{"name":"...","count":1,"results":[...],"identity_verified":false}`.
Each matching record contains `ans: {name,agent_host,version,verification:"unverified"}`.
It returns 404 for unknown or suppressed names, and 400 for invalid syntax.
Multiple claimed matches are returned without asserting which is authoritative. The response includes `count` (all matches), `ambiguous` (more than one claimant), and bounded `results`. Optional `limit` (1–20, default 20) and `offset` (0–100000, default 0) allow deterministic pagination through collisions. `truncated` is true when more matches follow this page. Invalid pagination returns 400.

A future independent DNS identity verifier must check DNSSEC, certificates,
transparency proofs, trust anchors and freshness before any identity claim
is labelled verified. Linux Foundation ANS v2 currently specifies a stricter
`ans://v{semver}.{agentHost}` wire format. SearchA2A's broader publisher
names are *not* automatically standards-compliant or verified, and no
version rewriting is performed.

## Static GitHub Pages endpoints

- `GET /api/v1/catalog.json`: latest bounded crawl snapshot, **not** the full persistent registry.
- `GET /api/v1/agents/{id}.json`: per-agent records from that snapshot.
- `GET /api/v1/azphalt-listings.json`: read-only, non-installable Azphalt export from that snapshot, mirrored to the `agent-feed` Git branch. `schema` stays `1`, every listing keeps `verification: "card_read_only"` and `installable: false`, and no existing key changes. Schema revision `1.1` (top-level `"schema_revision": "1.1"`) adds, per listing: `access` (`auth_types`, `keyless`, `payment_required`), `backing` (`declared_provider` `{organization, url_host}`, `hosting` platform label, `model_provider` name), and, only when a fresh evidence export was available and has not expired, `verification_detail` (`level`, `level_name`, `reviewed`, `checked_at`, `expires_at`) and `registry_checked_at`. Absence of `verification_detail` means level 0. Consumers that pin `schema === 1` keep working; consumers that whitelist keys simply do not see the new ones until updated. Check `expires_at` before showing a level.
- MCP: see [MCP.md](MCP.md) for the read-only `POST /mcp` server.

These static endpoints are periodically rebuilt; missing cards in a snapshot may still exist in the persistent registry.

## Retired on-demand search

`POST /api/v1/search` returns **410 Gone** and never dispatches an Action. Existing `GET /api/v1/results/{uuid}` and authenticated `POST /internal/results` are temporarily retained to finish/search historical requests, subject to their seven-day mailbox expiry. New searches never create those mailboxes.

All agent metadata is unverified third-party content, not proof that an endpoint is currently operational or safe.
