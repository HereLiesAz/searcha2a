# SearchA2A

Find public A2A Agent Cards and skills through a persistent, searchable registry.

**[Search the registry](https://searcha2a.azphalt.store/)**

## Search

Enter an agent name, skill, or capability and select **Search agents**. Results come directly from SearchA2A's registry. Search does **not** start GitHub Actions, fetch third-party sites, or require Cloudflare Turnstile. Searches may return no matches.

The registry survives crawler failures and retains previously discovered cards. Administrators can independently approve, hide, reject, or deactivate listings; crawler updates cannot override those decisions. Hidden and rejected cards are omitted from the next scheduled Pages and Azphalt feed publication. Curation uses a separate admin credential. Its contents are metadata only: a published card does **not** prove agent availability, identity, security, or A2A protocol conformance.

## How discovery works

Scheduled [GitHub Actions indexing](.github/workflows/pages.yml) reads the curated seeds and compatible public registries in [sources.json](sources.json), including [a2aregistry.org](https://a2aregistry.org), and uses GitHub discovery when available. The crawler reads Agent Cards without invoking their advertised execution endpoints. Each successful batch is merged into a persistent SQLite-backed Cloudflare Durable Object. Crawls of up to 150 cards use uploads capped at 70 cards and 1 MB, preserving all suppression evidence. An empty or failed crawl never deletes existing agents.

GitHub Actions runs when the source changes, on the daily crawl schedule, or when an operator manually starts indexing. **A user's search never consumes a workflow slot.**

## ANS names

Publisher Agent Cards may supply `ansName` and `agentHost`, e.g.
`ans://release-2026.11.weather.example.com`. Versions are preserved as
provided, without SemVer rules or an inserted `v`.
`GET /api/v1/ans/resolve?name=...` looks up stored name claims, never
confusing them with proof of identity. DNS, certificates and signed
transparency evidence require a separate verifier.

## Interfaces

- [Search API and agent details](docs/API.md): `GET https://searcha2a-api.hereliesaz.workers.dev/api/v1/agents?query=weather`.
- [Published crawl snapshot](https://searcha2a.azphalt.store/api/v1/catalog.json): static JSON for the latest indexing run, distinct from the persistent registry.
- [Azphalt external-agent directory](https://azphalt.store/agents): read-only discovery listings, not installable Azphalt packages.

The legacy `POST /api/v1/search` dispatch route now returns **410 Gone**. Existing result UUIDs remain readable until their seven-day retention expires. The separate `/api/v1/directory?q=...` semantic API remains available for clients such as Aive. It ranks candidates from the persistent curated registry with optional Workers AI; it also never triggers a crawl.

See [API](docs/API.md), [Architecture](docs/ARCHITECTURE.md), and [Operations](docs/OPERATIONS.md) for protocol, security, and deployment details.
