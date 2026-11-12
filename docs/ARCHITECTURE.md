# SearchA2A architecture

[User guide](../README.md) · [API](API.md) · [Operations](OPERATIONS.md)

## Data flow

```text
Scheduled or manually triggered Pages indexing (GitHub Actions)
  -> discover.py: curated public HTTPS Agent Cards, registries, GitHub leads
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
| Crawling never invokes advertised execution endpoints | `src/discover.py`: `fetch_json` and `normalize_card` |
| Card fetches are limited to public HTTPS and reject redirects | `src/discover.py`: `require_public_https`, `NoRedirect`, `fetch_json` |
| Store entries remain read-only and non-installable | `src/azphalt_listings.py`: `build_listings` |

## Limits and caveats

The current scheduled collector reads up to 90 card candidates and publishes up to 70 normalized cards per run. It also reads up to two pages of 30 agents from each explicitly configured paginated registry, including a2aregistry.org. Those are **per-crawl** limits, not a cap on accumulated registry records. The persistent registry retains IDs across runs. A new crawl does not invalidate a card it fails to rediscover, so stale entries are possible; curator hide/reject/deactivate decisions persist across crawls. Publishing filters the static Pages catalog and Azphalt export using the signed-in crawler's authenticated suppression manifest (and fails closed if it is missing); statuses propagate to the snapshots on the next index run, not immediately; there is no health probe, verified ownership, explicit self-registration, global search-engine crawl, or automatic removal policy yet.

SQL keyword search scans stored normalized name/skill/description/endpoint text and ranks by name, skill and description hits. The separate semantic endpoint ranks up to 100 visible persisted candidates with optional Workers AI; a `truncated` response signals incomplete coverage when the registry has more than 100 visible entries. It does not fetch static feeds or third-party sites. A single Durable Object coordinates the registry; future high-traffic growth may require sharding or migrating to Cloudflare D1.

The crawler's DNS safety checks happen before HTTP connection, so DNS rebinding remains a known limitation. Query output is third-party metadata only.

## Deployment ordering

Gateway deployment creates the SQLite registry class. Pages indexing attempts authenticated ingest with bounded retries to tolerate simultaneous push-triggered gateway and Pages workflows. A failed ingest **fails the Pages job**, rather than falsely declaring the persistent registry current. The gateway smoke test checks the public registry's JSON route, but does not itself prove a crawl was uploaded; end-to-end verification requires the scheduled ingest step to succeed and a real query to return stored records.
