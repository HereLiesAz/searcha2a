# SearchA2A architecture

[User guide](../README.md) · [API](API.md) · [Operations](OPERATIONS.md)

## Data flow

```text
Scheduled or manually triggered Pages indexing (GitHub Actions)
  -> discover.py: curated HTTPS cards, domain probes, AI Catalog/WebFinger/ERC-8004 links, opt-in authorized feeds, registry and GitHub leads
  -> bounded discovery catalog JSON
  -> registry_sync.py (existing private shared secret)
  -> Worker POST /internal/registry/ingest
  -> SQLite-backed AgentRegistry Durable Object (persistent upsert)
  
Browser / public client
  -> Worker GET /api/v1/agents?query=...
  -> AgentRegistry SQL query
  -> JSON response immediately, without Actions or external crawl

Aive / semantic client
  -> GET /api/v1/directory?q=...
  -> persistent registry visible candidates + optional Workers AI embedding
  -> semantic or keyword-fallback metadata results, without Actions
```

In parallel, the Pages job publishes the latest **bounded crawl snapshot** as `/api/v1/catalog.json`, per-card JSON, and the Azphalt non-installable directory feed. This static snapshot is **not** the complete accumulated registry.

## Original decisions and why they changed

- **Why GitHub Actions originally ran every search:** GitHub code discovery needed token-protected APIs; a Worker mediated dispatch to avoid exposing an Actions-write credential to browsers. Turnstile and per-IP rate limits were needed to protect this expensive public operation.
- **Why per-query dispatch was retired:** GitHub Actions startup, callback polling, and occupied workflow slots caused unnecessary latency and contention. Searches now run only against our own persistent index. Actions is used **only** for scheduled/push/manual indexing.
- **Why the original result mailbox used SQLite Durable Objects:** Workers KV propagation could delay callback visibility across regions. Existing seven-day result mailboxes remain readable for old requests but no new searches create them.
- **Why registry review is independent of crawler ingest:** A discovered Agent Card is untrusted input; giving the indexing credential permission to approve or reactivate agents would erase the approval boundary. Separate administrator authorization, durable review state and audit history keep curator decisions intact across crawls.
- **Why the registry also uses SQLite Durable Objects:** `AgentRegistry` stores normalized cards durably and atomically upserts scheduled batches without wiping unseen records. Its SQL interface provides immediate public reads with no GitHub dependency. `v2` in `worker/wrangler.toml` introduces this class without destroying the `v1` result class.
- **Why the Azphalt feed is mirrored to GitHub raw CDN:** A same-zone Cloudflare Worker subrequest to the SearchA2A Pages hostname previously returned 502. Keeping the existing `agent-feed` mirror avoids changing that working integration.
- **Why the semantic directory is retained and moved to SQL:** It was introduced independently for clients such as Aive using the published snapshot to avoid per-search GitHub Actions. Now it ranks visible persisted registry candidates to share moderation, naming and freshness with keyword search; it still does not invoke Actions or external agents.
- **Why metadata stays non-installable:** Discovering an Agent Card does not establish runtime health, ownership, conformance, or authorization. The export cannot be represented as a signed `.azp` package.

## Unified ANS name layer

The naming, capability search, and verified-identity concepts share one
SearchA2A registry. Publishers control their version format, encoded as
`ans://<version>.<agentHost>`, with no inferred SemVer or `v` prefix.
The Agent Card declares `ansName` and `agentHost` explicitly so the
version-to-domain boundary is never guessed.

`worker/ans.mjs` checks only DNS-name syntax and that the claimed host
matches the explicit host. `AgentRegistry` retains the untrusted claim
beside its stable record ID, indexes it, and exposes read-only
`GET /api/v1/ans/resolve`, with bounded results and explicit ambiguity reporting. Curation and suppression apply identically
to keyword search and name lookup. No read makes external network calls;
claims never become `verified` by ingest or moderation.

The separately deployed identity verifier will eventually check the
Linux Foundation ANS DNS discovery profile, certificate binding, signed
transparency evidence, freshness and revocation. Its current ANS v2 wire
syntax mandates `ans://v{numeric-semver}.{agentHost}`; broader opaque
publisher versions can be locally indexed but must not be misrepresented
as conforming to that external standard without actual verification.
Azphalt package signing remains a separate authority.

## Components

| Component | Source |
| --- | --- |
| Browser search | `site/app.js`, `site/index.html` |
| Persistent semantic discovery | `worker/semantic.mjs`, `GET /api/v1/directory` |
| Registry SQL and HTTP routes | `worker/index.mjs` (`AgentRegistry`, `handle`) |
| Durable Object migration | `worker/wrangler.toml` (`v2`) |
| Scheduled discovery | `src/discover.py`, `sources.json`, `.github/workflows/pages.yml` |
| Authenticated sync | `src/registry_sync.py`, `POST /internal/registry/ingest` |
| Azphalt export | `src/azphalt_listings.py` and `agent-feed` mirror |
| Deployment smoke checks | `.github/workflows/gateway.yml` |
| Regression tests | `tests/registry.test.mjs`, `tests/test_registry_sync.py`, existing test suites |

## Invariants and enforcement

| Invariant | Enforcement |
| --- | --- |
| No search can start a GitHub workflow | `worker/index.mjs`: `POST /api/v1/search` returns 410; `site/app.js` only uses GET |
| Only a separately authenticated administrator can approve, reject or deactivate a listing | `worker/index.mjs`: `REGISTRY_ADMIN_SECRET` check, `AgentRegistry.fetch` moderation and audit endpoints |
| Static Pages and Azphalt publication cannot reintroduce suppressed entries | `AgentRegistry.fetch` scheduled ingest response `suppressed_ids`; `src/registry_sync.py`: `filter_export_catalog` before export |
| Hidden, deactivated and rejected cards cannot appear in public live search or direct detail | `AgentRegistry.fetch`: active/non-rejected query filter and agent lookup |
| Only authorized scheduled crawls can mutate the registry | `worker/index.mjs`: `POST /internal/registry/ingest` checks shared secret |
| A batch is all-or-nothing and does not delete unseen agents | `AgentRegistry.fetch`: input validation before `storage.transactionSync`, `INSERT ... ON CONFLICT DO UPDATE` |
| Public search is bounded | `AgentRegistry.fetch`: 120-char query, up to 12 terms, limit <= 100, bounded offset |
| Crawling never invokes advertised execution endpoints | `src/discover.py`: `card_references`, `unpack_cards`, `fetch_json` (GET only and one hop) |
| Private catalogs require explicit publication permission, and credentials never leave the configured feed request | `src/discover.py`: `private_catalogs` with `publish is True`, `fetch_json(private_auth=...)`, `NoRedirect`, no authenticated link traversal; `.github/workflows/pages.yml` passes the secret only to the crawler |
| Card fetches are limited to public HTTPS, JSON media types and reject redirects | `src/discover.py`: `require_public_https`, `NoRedirect`, `fetch_json` |
| Store entries remain read-only and non-installable | `src/azphalt_listings.py`: `build_listings` |

## Limits and caveats

The current scheduled collector reads up to 350 initial candidate URLs (including up to 50 reserved GitHub leads) and at most 100 one-hop manifest links and publishes up to 150 normalized cards per run. It also reads up to five pages of 30 agents from each explicitly configured paginated registry, including a2aregistry.org. Configured bulk JSON feeds rotate through at most 150 entries per run; paginated APIs that report a bounded numeric `total` rotate their five-page window across days. Neither mechanism resets accumulated registry rows. Those are **per-crawl** limits, not a cap on accumulated registry records. The persistent registry retains IDs across runs. A new crawl does not invalidate a card it fails to rediscover, so stale entries are possible; curator hide/reject/deactivate decisions persist across crawls. Publishing filters the static Pages catalog and Azphalt export using the signed-in crawler's authenticated suppression manifest (and fails closed if it is missing); statuses propagate to the snapshots on the next index run, not immediately; there is no health probe, verified ownership, explicit self-registration, global search-engine crawl, or automatic removal policy yet.

SQL keyword search scans stored normalized name/skill/description/endpoint text and ranks by name, skill and description hits. The separate semantic endpoint ranks up to 150 visible persisted candidates with optional Workers AI; a `truncated` response signals incomplete coverage when the registry has more than 150 visible entries. It does not fetch static feeds or third-party sites. A single Durable Object coordinates the registry; future high-traffic growth may require sharding or migrating to Cloudflare D1.

Semantic search batches Workers AI text embeddings in groups of at most 99 candidates plus
one query (100 strings per model request). A failure in any embedding batch falls back to
keyword ranking for the whole query rather than mixing incompatible partial results.
When an embedded registry row and an independently fetched card advertise the same
endpoint, fresh publisher-origin proof evidence takes precedence; no unverified
registry row can suppress it. Neither ranking nor this provenance precedence
establishes Linux Foundation ANS identity, approved execution, or Azphalt signing.

The crawler only accepts HTTP GET application/json (or application/*+json) documents over HTTPS, rejects HTTP redirects and private DNS results, limits ordinary card documents to 512 KiB, and follows at most 100 explicit card links one hop from bounded manifests. The domain matrix includes 11 JSON paths; known WebFinger accounts are limited to eight per configured domain. Probe domains, assistant IDs, and WebFinger accounts are explicitly configured. The crawler reads AI Catalog typed A2A entries (including bounded inline subcatalogs), WebFinger JRD self links and ERC-8004 registration services; it does **not** assert WebFinger or ERC-8004 identity verification. Private/enterprise HTTPS JSON feeds (up to five, 8 MiB each) are supported with one-feed-only bearer or X-API-Key credentials and **explicit `publish: true`**, never implicit or wildcard access. All linked card fetches are unauthenticated and use the ordinary public HTTPS guard; private raw card JSON is never published, but an opted-in feed's normalized names, skills, endpoints and source URL are public. No wildcard enumeration, task execution, authenticated extended-card retrieval, on-chain events, DNS TXT sweeps, MCP tool calls or enterprise service discovery without configured access. Missing skills are retained only when an explicit v1 supportedInterface identifies an agent. Raw public card JSON is retained only when <= 4 KiB; otherwise its SHA-256 is recorded. Raw fields are untrusted and **not** proof of A2A conformance or JWS integrity. DNS safety checks happen before HTTP connection, so DNS rebinding remains a known limitation. Query output is third-party metadata only.

## Deployment ordering

Gateway deployment creates the SQLite registry class. Pages indexing attempts authenticated ingest with bounded retries to tolerate simultaneous push-triggered gateway and Pages workflows. A failed ingest **fails the Pages job**, rather than falsely declaring the persistent registry current. The gateway smoke test checks the public registry's JSON route, but does not itself prove a crawl was uploaded; end-to-end verification requires the scheduled ingest step to succeed and a real query to return stored records.
