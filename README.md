# SearchA2A

Public discovery for A2A Agent Cards, powered by **GitHub Actions**.

**Search button → Cloudflare request gateway → GitHub Actions search → result callback → searchable page and JSON API.** Every user query launches its own bounded workflow run. The Worker only authenticates and dispatches the work, then holds its results. A separate daily Actions workflow publishes a fresh static catalog to GitHub Pages.

## Interfaces

| Interface | Purpose |
|---|---|
| GitHub Pages `https://searcha2a.azphalt.store/` | Search Agent Cards by natural-language keywords, skills and capabilities. |
| `POST /api/v1/search` (Worker) | Trigger a new Actions run; JSON `{"query":"forecast","turnstile":"<challenge-token>"}`. Returns HTTP 202 with request ID. |
| `GET /api/v1/results/{id}` (Worker) | Poll queued search and retrieve full results JSON. Results expire after 7 days. |
| GitHub Pages `https://searcha2a.azphalt.store/api/v1/catalog.json` | Read-only, daily-generated public catalog snapshot; no token required. |
| GitHub Pages `https://searcha2a.azphalt.store/api/v1/azphalt-listings.json` | Automatically generated, read-only external-agent listings for Azphalt Store. |

Card results provide agent name, description, skills/tags, protocol bindings, endpoint, source, timestamp and verification level. Card validation isn't proof of a functioning remote agent. No invocation or execution of third-party agents is performed.

## Azphalt Store integration

The daily Pages workflow generates `api/v1/azphalt-listings.json` from the same crawl, one listing per valid deduplicated Agent Card. Azphalt Store consumes this via its read-only `/api/a2a/agents` proxy and `/agents` directory. Listings are explicitly `kind: "a2a-agent"`, `installable: false`, and `verification: "card_read_only"`. They are **not** signed `.azp` packages, LLMs, advertised free purchases, or first-party trusted agents. The Store never downloads or runs third-party agents.


## Domain

The public hostname is `https://searcha2a.azphalt.store/`; its DNS-only CNAME must point to `hereliesaz.github.io`, **not** to the repository path. The GitHub Pages custom domain must also be registered in Pages settings. The repository-local **Configure searcha2a.azphalt.store** Action can perform both operations after its credentials are configured; it refuses to overwrite conflicting DNS records. GitHub Actions Pages deployments do not use a repository `CNAME` file.

The Actions-dispatch API remains on a separate Cloudflare Worker hostname, configured via the repository variable `SEARCHA2A_API_URL`. No credentials are published by the static site.

## Setup

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the deployment configuration, security boundaries, secrets, and local tests. Deploying the Cloudflare Worker requires the owner's Cloudflare account; a static GitHub Page cannot hold the write-authorized GitHub token necessary to dispatch workflows.

## Sources

Edit [sources.json](sources.json) to add public Agent Card URLs and compatible public JSON registry endpoints. GitHub code search contributes additional A2A card candidates. The crawler understands current `/.well-known/agent-card.json` and legacy `/.well-known/agent.json` metadata, including GitHub-hosted JSON files. Registry formats are not standardized, so unfamiliar APIs need dedicated adapters.

- [A2A Agent Card specification](https://a2a-protocol.org/latest/specification/agent-card/)
- [Solo.io: Agent Discovery, Naming, and Resolution](https://www.solo.io/blog/agent-discovery-naming-and-resolution---the-missing-pieces-to-a2a)
- [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)

## Local tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.mjs
```
