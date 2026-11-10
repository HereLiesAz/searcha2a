# Architecture and security boundaries

The user submits a query from GitHub Pages. A Cloudflare Worker validates the browser origin, a Turnstile token, the query length, and its rate-limit binding. It stores a random request UUID in Workers KV, then dispatches `.github/workflows/search.yml` through the GitHub API with a Worker-only token granting **Actions: write** on this repository. The GitHub Actions runner collects public A2A Agent Cards from curated seeds, registered JSON registries and GitHub code search. A callback, authenticated by `RESULT_CALLBACK_SECRET`, stores the result against the pending request ID. The browser polls `GET /api/v1/results/{id}` until complete or until its own wait expires. The search engine itself executes exclusively in Actions, never in the Worker.

A separate daily `pages.yml` Actions run generates `api/v1/catalog.json` from the same discovery code and deploys the static site with GitHub Pages. It also exports `api/v1/azphalt-listings.json` for the Azphalt Store external-agent directory, preserving original card URLs and skills while marking all listings `installable: false`. The Azphalt Store Worker proxies that static feed at `/api/a2a/agents` to obey the store's same-origin content-security policy; its `/agents` view never treats a card as a signed `.azp` or `kind: "llm"` package. Search results are ephemeral; the scheduled discovery index is the source for published store listings. This is a snapshot, not the live search implementation. Its static JSON, plus per-agent `api/v1/agents/{id}.json` resolution records, act as a public read-only API. The live API `POST /api/v1/search` creates a search, returning an ID and, when supplied by GitHub, its workflow-run URL; `GET /api/v1/results/{id}` returns the result.

## Trust and limitations

- The Worker alone stores the dispatch token. Public website JavaScript and GitHub artifacts contain no credentials.
- Browser origin checks are defense in depth, not authentication; Turnstile and the approximate per-client Cloudflare rate limit protect dispatch. A hosted service will still need an overall Actions-spend limit / monitoring. Rate limits aren't perfectly global.
- The result callback uses a separate secret and may update only a matching pending request. Results expire from KV after seven days.
- Fetching an Agent Card validates *metadata*, not A2A runtime conformance, identity proof, cryptographic provenance, or availability. `card_read_only` must never be displayed as `verified live`.
- The crawler uses explicit public HTTPS sources, DNS public-address checks, disallows redirects and proxies, and enforces size, count, and timeout limits. As with all DNS preflight guards, a validation-to-connect race remains possible; the public configuration must not include untrusted intranet addresses. No agent endpoint is invoked.
- Discovery is bounded rather than exhaustive. GitHub Search API indexing and token quotas, public registry formats, and remote availability limit coverage. Registry endpoints in `sources.json` must return JSON lists or objects with `agents`, `items`, `results`, `entries`, or `data` arrays. Other registry APIs require explicit adapters.
- The Worker is needed because GitHub's `workflow_dispatch` API requires Actions write permission. GitHub Pages cannot trigger it securely by itself. The Worker does **not** perform searches.
- No unauthenticated arbitrary-URL crawling, credential scraping, or arbitrary HTTP execution is allowed.

## Configuration

1. Configure the Pages site for **GitHub Actions** and attach the custom domain `searcha2a.azphalt.store` in **Settings → Pages**, with a DNS-only CNAME `searcha2a` → `hereliesaz.github.io`. For fully workflow-driven provisioning, configure repo secrets `PAGES_ADMIN_TOKEN` (fine-grained Pages + Administration write for this repository), `CLOUDFLARE_API_TOKEN` (DNS Write for the `azphalt.store` zone), and `CLOUDFLARE_ZONE_ID`, then manually run the repository-local **Configure searcha2a.azphalt.store** Action. The Action registers the GitHub Pages domain *before* publishing DNS, and refuses to overwrite conflicting records. GitHub Actions-based Pages deployment ignores `CNAME` files.
2. Create a Cloudflare KV namespace `RESULTS` (`npx wrangler kv namespace create RESULTS`) and replace the placeholder KV namespace ID in `worker/wrangler.toml`.
3. Create a Cloudflare Turnstile widget for `searcha2a.azphalt.store`.
4. Make a fine-grained GitHub PAT **scoped only to `HereLiesAz/searcha2a`** with **Actions: write**, and set it only as Worker secret `GITHUB_DISPATCH_TOKEN`. Set Worker secrets `TURNSTILE_SECRET` and `RESULT_CALLBACK_SECRET` via `npx wrangler secret put ...`.
5. Deploy the Worker through the **Deploy API Gateway** Actions workflow. Configure repository secrets `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` for that workflow. A local `cd worker && npx wrangler deploy` also works.
6. Set repository Actions *variable* `SEARCHA2A_API_URL` to the deployed worker HTTPS origin; set variable `TURNSTILE_SITE_KEY` to the public Turnstile site key. Set repository Actions *secret* `RESULT_CALLBACK_SECRET` to the same callback secret configured on the Worker.
7. Run **Reindex and publish Pages** once after the hostname is configured; the daily schedule maintains the static snapshot. The workflow skips deployment if Pages is not enabled. DNS and certificate issuance may take time to propagate.

To avoid granting an Actions token to public visitors, neither the token nor any ad hoc authorization header is accepted from the webpage. A request that isn't granted a validated Turnstile challenge never reaches GitHub Actions.

## Local verification

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs
node --check site/app.js
node --check worker/index.mjs
```

No third-party Python dependencies are required. The Worker uses the Cloudflare runtime and `wrangler` for deployment, not for searching.
