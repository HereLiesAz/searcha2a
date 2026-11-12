# SearchA2A operations

Implementation details: [ARCHITECTURE.md](ARCHITECTURE.md). Public API: [API.md](API.md). User instructions: [README.md](../README.md).

## Deployment inventory

| Component | Code or job | Deployed location |
| --- | --- | --- |
| Public web site and static JSON | `.github/workflows/pages.yml` | https://searcha2a.azphalt.store |
| Live dispatch and callback gateway | `.github/workflows/gateway.yml` and `worker/` | https://searcha2a-api.hereliesaz.workers.dev |
| Search executor | `.github/workflows/search.yml` | GitHub Actions on demand |
| Result mailbox | `SearchResult` in `worker/index.mjs` | SQLite-backed Cloudflare Durable Object, one per UUID |
| Store discovery feed | `src/azphalt_listings.py` and `pages.yml` | Pages JSON and mirrored `agent-feed` branch |

### Repository settings and secrets

**Repository Actions variables:**

| Name | Value |
| --- | --- |
| `SEARCHA2A_API_URL` | `https://searcha2a-api.hereliesaz.workers.dev` |
| `TURNSTILE_SITE_KEY` | The **public** site key for a Cloudflare Turnstile widget authorized for `searcha2a.azphalt.store` |

**Repository Actions secrets:** `CLOUDFLARE_API_TOKEN` (Worker deploy rights), `CLOUDFLARE_ACCOUNT_ID`, and `RESULT_CALLBACK_SECRET` (identical to the Worker secret). Domain automation additionally needs `PAGES_ADMIN_TOKEN`, `CLOUDFLARE_ZONE_ID` and an appropriately DNS-scoped Cloudflare token.

**Worker-only secrets:** `GITHUB_DISPATCH_TOKEN` (fine-grained token scoped to `HereLiesAz/searcha2a` with Actions: write), `TURNSTILE_SECRET` (private site secret), `RESULT_CALLBACK_SECRET` (same value as repository callback secret). Do not put these secrets in `site/config.json`, Pages artifacts, commits, test output or browser code.

## Configure domain and Cloudflare

GitHub Pages must use **GitHub Actions** as its build source and register the custom domain `searcha2a.azphalt.store`. Cloudflare DNS must have a **DNS-only CNAME** `searcha2a` → `hereliesaz.github.io`. The repo's manual [Configure searcha2a.azphalt.store](../.github/workflows/domain.yml) workflow can register Pages before creating the CNAME when its three domain-management credentials are configured. It refuses to overwrite conflicting DNS records. GitHub Actions Pages deployment does not rely on a committed `CNAME` file.

Create a Cloudflare **Managed** Turnstile widget allowing `searcha2a.azphalt.store`. Its site key is public; its secret is Worker-only. A browser challenge passing does not prove that GitHub dispatch or the result callback has completed.

## Publish and deploy

1. [Reindex and publish Pages](../.github/workflows/pages.yml) runs on pushes to `main`, daily at **03:29 UTC**, and manually. It builds the catalog, per-agent records, Azphalt listings, mirrors the store feed to `agent-feed` and writes the public `config.json` from Actions variables. Deployment requires GitHub Pages to be enabled.
2. [Deploy API Gateway](../.github/workflows/gateway.yml) runs on changes under `worker/**` or the gateway workflow on `main`, or manually. It deploys the `searcha2a-api` Worker through Wrangler and verifies the results route. It checks whether Cloudflare secrets are supplied; absent credentials cause deployment steps to be skipped, not an actual deployment.
3. [On-demand A2A search](../.github/workflows/search.yml) starts only through `workflow_dispatch`. A normal browser search uses this path; the job runs `src/discover.py` and then `src/report.py` to send a callback. The callback step uses `if: always()` to report failures as an empty result with warnings when possible.

Wrangler configuration in `worker/wrangler.toml` binds `RESULTS` to `SearchResult` and uses the `v1` SQLite Durable Object class migration. **No Workers KV namespace is required.** An existing unrelated `searcha2a-results` KV namespace is not used; its historical requests were not migrated. Pending requests from before the cutover must be retried.

For a manual local deployment with credentials available to Wrangler:

```sh
cd worker
npx --yes wrangler@4 deploy
```

If deploying from a different environment, configure the same Worker-only secrets using Wrangler's secret-management commands. Don't replace an existing production secret with a placeholder.

## Verification

**Automated checks** (also run by [Checks](../.github/workflows/ci.yml)):

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs
node --check site/app.js
node --check worker/index.mjs
```

**Deployment smoke test:** `gateway.yml` queries `GET /api/v1/results/{random_uuid}` and expects **404** with `{"error":"Search expired or unknown"}`. This verifies the public gateway route can reach the new mailbox binding. It does **not** create a request or test the callback.

**End-to-end test:** From [the website](https://searcha2a.azphalt.store), submit a term matching the published catalog (for example one in its `/api/v1/catalog.json`). Indexed matching cards should appear **before** the GitHub Actions request completes, alongside “More results are coming when GitHub Actions finishes.” Then inspect the newest `On-demand A2A search` run in [Actions](https://github.com/HereLiesAz/searcha2a/actions/workflows/search.yml). Verify both **Discover Agent Cards** and **Return results to request gateway** succeeded. When polling receives `status: "complete"`, the indexed cards must remain visible and any new live cards should be added without duplicates; the progress message must change to “Search complete.” An empty live result is allowed and must not remove indexed matches. Test a missing/invalid Turnstile challenge separately: cached matches should still appear, but no workflow may dispatch. Workflow success or static matches alone do not prove this full browser flow.

## Crawler coverage

The crawler uses `src/discover.py` and the explicit inputs in `sources.json`: `card_urls` for single published cards, `registry_urls` for known compatible JSON registries, and `paginated_registry_urls` for list APIs such as `https://a2aregistry.org/api/agents`. A paginated source is queried with `limit=30&offset=0` and at most one more page for scheduled snapshots; live searches use one page and also supply `search=<query>`. The records are **discovery leads**, not proof. Only the original `wellKnownURI` is fetched as a candidate card, with redirects disabled and public HTTPS/DNS restrictions. The crawler never calls the advertised agent execution endpoint.

The daily full-catalog build requests an empty query and the live workflow passes the browser's search term. A registry being unreachable is nonfatal and reported in `warnings`; it does not invalidate healthy seeds or other sources. The first 90 deduplicated candidates are read and at most 70 normalized records are published. These bounds can exclude valid agents, and the public index is a snapshot rather than a full directory.

Review the latest [Reindex and publish Pages](../.github/workflows/pages.yml) job for the discovered count and warnings, and inspect `/api/v1/catalog.json` for the actual `warnings` array. A source must be a public HTTPS JSON API, not an HTML directory. Add new adapters only after inspecting real API shapes; avoid scraping unsupported formats or bypassing safety validation.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Site says live search awaits gateway configuration | Pages artifact `config.json`; repository variables `SEARCHA2A_API_URL` and `TURNSTILE_SITE_KEY`; republish Pages |
| Cloudflare verification cannot complete | Widget hostname, correct public site key, private Worker secret, browser challenge blockers |
| `403 Origin not allowed` | Browser website must originate from `https://searcha2a.azphalt.store` |
| `403 Challenge verification failed` | Invalid, expired or mismatched Turnstile response; verify site/secret pairing |
| `429 Search rate limit exceeded` | Per-client Cloudflare limiter is configured for five requests per minute |
| `503 Gateway not configured` | Worker secrets or required bindings missing |
| `502 GitHub Actions rejected dispatch` | Fine-grained `GITHUB_DISPATCH_TOKEN` scope and Actions write permission |
| Run succeeds, site still displays queued results | Check `Return results to request gateway` step, callback secret parity, request UUID, and new result GET; old KV requests cannot be read after migration |
| Search returns no results | May be genuine; review workflow warnings and source coverage |
| Public catalog seems stale | Run Pages reindex and inspect its logs, source availability and published `generated_at` |
| Azphalt Store agents page returns 502 | Investigate Azphalt's `/api/a2a/agents` proxy and mirrored `agent-feed` data separately; it is not the live search Worker |

The browser checks status every three seconds for up to 48 attempts. The first set of cards is filtered locally from the previously published catalog and requires no live API; after Turnstile verification the queued GitHub Action supplies any additional cards. Results are not streamed. Seven-day mailbox expiry is implemented in `SearchResult` via a storage alarm, independent of the browser timer.

## Security and limitations

Turnstile and the per-IP limiter protect dispatch, but do not enforce a global GitHub Actions budget. Rate limiting is approximate and workflows consume account resources. Search metadata is public and result URLs are readable to anyone holding the UUID; avoid sensitive search content.

The collector reads curated public HTTPS URLs only. Advertised agent endpoints are not called or health-checked, and any card shown should be treated as third-party metadata, not verified software.
