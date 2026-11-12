# SearchA2A operations

[User guide](../README.md) · [API](API.md) · [Architecture](ARCHITECTURE.md)

## Production

| Service | Location |
| --- | --- |
| Browser | https://searcha2a.azphalt.store |
| Persistent registry | https://searcha2a-api.hereliesaz.workers.dev/api/v1/agents |
| Registry semantic directory | https://searcha2a-api.hereliesaz.workers.dev/api/v1/directory?q=weather |
| Latest crawl snapshot | https://searcha2a.azphalt.store/api/v1/catalog.json |
| Azphalt external-agent directory | https://azphalt.store/agents |

## Automation

- [Reindex and publish Pages](../.github/workflows/pages.yml): on pushes to main, daily at **03:29 UTC**, or manually. Crawls, syncs normalized cards into the persistent registry, exports the bounded Pages snapshot and Azphalt directory.
- [Deploy API Gateway](../.github/workflows/gateway.yml): on Worker or gateway workflow changes to main, or manually. Deploys `AgentRegistry` with `v2` SQLite migration and checks `GET /api/v1/agents?limit=1`. It also retains the legacy `SearchResult` `v1` class for pending seven-day mailboxes.
- **No per-query workflow.** Clicking Search executes a public GET against existing registry records. The retired POST dispatch responds 410. The separate semantic `/api/v1/directory` endpoint ranks up to 150 visible records from the persistent registry with optional Workers AI, not Actions. It fails closed if the registry binding is absent and flags truncated candidate sets.

## Settings and credentials

**Repository Actions variable:** `SEARCHA2A_API_URL=https://searcha2a-api.hereliesaz.workers.dev`. The Worker also retains the `AI` binding and `SEARCH_RATE_LIMITER` for the separate semantic directory endpoint.

**Repository Actions secrets:** `CLOUDFLARE_API_TOKEN` (Worker deployment), `CLOUDFLARE_ACCOUNT_ID`, and `RESULT_CALLBACK_SECRET` (reused as the authenticated registry ingestion secret). The last value must match the **Worker secret** `RESULT_CALLBACK_SECRET`. The existing `GITHUB_DISPATCH_TOKEN`, `TURNSTILE_SECRET` and `TURNSTILE_SITE_KEY` are no longer required for public searches; do not delete or rotate them during a deployment without accounting for legacy requests and other consumers.

GitHub Pages uses GitHub Actions for build/deploy and `searcha2a.azphalt.store` with a DNS-only CNAME `searcha2a` → `hereliesaz.github.io`. The manual `.github/workflows/domain.yml` can configure domain records with `PAGES_ADMIN_TOKEN`, `CLOUDFLARE_ZONE_ID` and Cloudflare DNS credentials. This is independent of search.

## Rollout

1. Merge the Worker and Pages changes to main. Gateway deployment adds `AgentRegistry` in the `v2` SQLite migration, preserving the legacy result object.
2. The Pages indexing job runs automatically on merge. Its `Ingest scheduled crawl into persistent registry` step retries temporary gateway errors and fails on auth/validation errors. If the gateway was not ready during the initial run, rerun Pages after the gateway workflow succeeds.
3. Check `GET /api/v1/agents?limit=1` returns HTTP 200 and a numeric `total` above zero after ingest. Search for a published card and check that the result appears without any new `On-demand A2A search` workflow.
4. Confirm the Pages snapshot and Azphalt `agent-feed` mirror still publish normally.

Existing `src/report.py` and historical result callbacks are for legacy in-flight requests only. They are not called by the website after migration.

## ANS name claims

`GET /api/v1/ans/resolve?name=ans%3A%2F%2Frelease-2026.agent.example.com`
is a public, read-only query against the persistent, curated registry.
The crawler stores publisher-supplied `ansName` + `agentHost` claims,
which are neither signed attestations nor verified DNS identities.
The response reports `identity_verified:false`; moderated or rejected
entries remain hidden. No DNS lookup, task invocation, or GitHub workflow
happens at query time. This change does not provision a verifier or its keys.

## Registry administration

Set a distinct Worker-only `REGISTRY_ADMIN_SECRET` before enabling curation. **Do not reuse** `RESULT_CALLBACK_SECRET` from GitHub Actions. In the Cloudflare Worker project, use `npx --yes wrangler@4 secret put REGISTRY_ADMIN_SECRET`; do not commit a secret or add it to the public Pages config. Registry search and scheduled ingest continue to function without this optional credential; admin operations fail closed with HTTP 401.

A curator may POST `{"id":"<20 hex characters>","action":"deactivate","reason":"Reason"}` to `/internal/registry/manage` using `Authorization: Bearer <REGISTRY_ADMIN_SECRET>`. Valid actions are `approve`, `reject`, `reset_review`, `hide`, `deactivate`, `activate`. Inspect `GET /internal/registry/audit/{id}` with the same credential for the recent decision history. Do not log or paste secret values. Browser and crawler clients cannot modify review states.

Newly ingested records default to active/unreviewed. Existing records are upgraded in place on Worker rollout; records and their original IDs remain intact. Public registry GETs hide deactivated, hidden, and rejected records, including direct-ID lookups. An ordinary scheduled crawl **cannot** reactivate a deactivated record or reverse a rejection. Approval is metadata-only review, not a signed Azphalt Store listing or proof of agent ownership.

**Publishing:** curation takes effect immediately in the live registry. The next Pages reindex retrieves suppression IDs from the authenticated registry ingestion response and removes those cards from the static catalog, per-agent records, and Azphalt export. The job fails closed if the response does not contain a valid suppression manifest, preventing a rollout race from republishing a hidden card. A curator can manually run **Reindex and publish Pages** to propagate a decision sooner; no ordinary search triggers any Action. Approved cards remain metadata-only and are **not** signed publisher attestations.

## Automated verification

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs
node --check site/app.js
node --check worker/index.mjs
```

New tests cover SQL persistence, atomic rejection of bad ingest batches, authorization, pagination, query matching, no workflow dispatch, HTTP sync retries, manifest deduplication, safe domain paths, HTML fallback rejection, non-strict card handling, and bounded one-hop discovery. The gateway smoke test verifies the registry binding responds but not the number of cards; a Pages ingest run and subsequent public GET prove data actually reached storage.

## Public crawler targets

[`sources.json`](../sources.json) defines `card_urls` (direct single cards or JSON manifests), `domain_urls` (explicit HTTPS origins probed at seven canonical/legacy paths), `assistant_ids` (optional known assistant IDs keyed by exactly matching HTTPS origin), `registry_urls` (bounded JSON feeds) and `paginated_registry_urls` (offset-based public REST feeds), `rotate_registry_feeds` (daily rotating bounded slices of JSON arrays), and `rotate_paginated_registry` (daily page windows when the API reports a sane numeric total). For reproducible diagnostics, `rotation_index` may override the UTC ordinal day. The old `www.a2aregistry.org/registry.json` URL currently serves an SPA rather than a JSON feed and is deliberately not configured; the live `a2aregistry.org/api/agents` REST API remains indexed. Invalid, missing, non-JSON or forbidden sources are skipped; unavailable registries contribute warnings but cannot erase accumulated records. Credentials, private enterprise catalogs, paid task invocations, A2A/MCP POST discovery and ERC-8004 event scraping are deliberately excluded. If a source exposes only HTML or requires login, add a reviewed read-only JSON adapter rather than scraping arbitrary pages.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Website says registry API unavailable | `site/config.json` output; `SEARCHA2A_API_URL` repository variable; Pages deployment |
| `503 Registry not configured` | Gateway deploy and Wrangler `REGISTRY` Durable Object binding / `v2` migration |
| Index ingest returns `401` | `RESULT_CALLBACK_SECRET` Actions/Worker secret parity |
| Ingest fails during simultaneous deployment | Deploy API Gateway, then rerun Pages |
| Search finds no agents | Check `/api/v1/agents?limit=1` and last Pages ingest; the requested terms may be absent |
| Search does not include a known card | Check it exists in the accumulated registry, not just an older snapshot; examine crawler warnings |
| Azphalt feed is stale | Inspect Pages export and raw GitHub `agent-feed` mirror; its snapshot is separate from persistent registry |
| Search starts GitHub Actions | Regression: the website should only GET `/api/v1/agents`, and `POST /api/v1/search` must return 410 |

## Security and limitations

The public API is intentionally read-only; browsers cannot submit or execute agents. Only the private authenticated crawler upload writes the registry. Agent Cards are untrusted descriptive metadata, not verified agent endpoints. Public result IDs and card URLs must not be treated as authorization tokens.

Current per-crawl bounds: 350 initial candidates (up to 50 reserved GitHub leads), 100 one-hop manifest links, 150 normalized cards, at most 12 configured probe domains with seven standard paths each and up to ten explicitly listed assistant IDs per domain, up to five paginated registry pages, 512 KiB per Agent Card, seven seconds per source, and ten concurrent card reads. Scheduled registry uploads use 70-card / 1-MB batches and combine all suppression responses. Explicit curated registry feeds allow up to 8 MiB of JSON; the fixed promotion feed alone allows up to 16 MiB. These bounds do not limit the persistent registry's cumulative record count, but discovery remains incomplete. Indexed records are retained until a deliberate future removal policy is implemented.
