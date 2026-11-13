# SearchA2A

Find public A2A Agent Cards and skills through a persistent, searchable registry.

**[Search the registry](https://searcha2a.azphalt.store/)**

## Search

Enter an agent name, skill, or capability and select **Search agents**. Results come directly from SearchA2A's registry. Search does **not** start GitHub Actions, fetch third-party sites, or require Cloudflare Turnstile. Searches may return no matches.

The registry survives crawler failures and retains previously discovered cards. Administrators can independently approve, hide, reject, or deactivate listings; crawler updates cannot override those decisions. Hidden and rejected cards are omitted from the next scheduled Pages and Azphalt feed publication. Curation uses a separate admin credential. Its contents are metadata only: a published card does **not** prove agent availability, identity, security, or A2A protocol conformance.

## How discovery works

Scheduled [GitHub Actions indexing](.github/workflows/pages.yml) also performs up to three fixed-topic, metadata-only searches daily against the public allagents directory, checking any discovered cards at their publisher origins (never A2A agent tasks, relays, registration or purchases). It can read explicitly approved, digest-checked AGNTCY OCI Directory artifacts and opt-in MCP A2A metadata resources (never MCP tools or A2A tasks). It resumes bounded ERC-8004 IdentityRegistry scans on Ethereum and Base from durable, confirmed-block checkpoints, committed only after successful registry ingestion, alongside curated Agent Cards (including the explicit direct URLs in [the crawler guide](docs/crawler_target_guide.md)), explicit domain path probes, JSON multi-agent manifests, opt-in NAIS DNS TXT pointers, typed AI Catalogs, ERC-8004 registration links, opt-in WebFinger identities, public registry feeds and GitHub leads in [sources.json](sources.json), including [a2aregistry.org](https://a2aregistry.org). Domain probing includes canonical and legacy well-known paths; manifests may supply one additional layer of public card URLs. The crawler reads Agent Cards without invoking their advertised execution endpoints. Bounded daily rotation of public registry feeds/pages broadens accumulated coverage without increasing the per-run publication cap. Unavailable public Agent Card leads from rotating sources enter a bounded, persistent retry queue with exponential day-based backoff, prioritizing on-chain and registry discoveries over GitHub repository examples. Retry state is saved only after confirmed registry ingestion; raw private catalog URLs never enter the public queue. The latest run's aggregate source-fetch health is published at [`/api/v1/crawler-health.json`](https://searcha2a.azphalt.store/api/v1/crawler-health.json), with no private hostnames, credentials or card metadata. Each successful batch is merged into a persistent SQLite-backed Cloudflare Durable Object. Crawls of up to 150 cards use uploads capped at 70 cards and 1 MB, preserving all suppression evidence. An empty or failed crawl never deletes existing agents.

An optional authorized HTTPS catalog can be connected using a GitHub Actions secret. Private sources default to **not published**; only explicitly opted-in, authorized feeds may contribute sanitized Agent Card metadata to the public registry. Credentials and private raw card documents are never exported. See [Operations](docs/OPERATIONS.md).

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

- [MCP server](docs/MCP.md): `POST /mcp` lets a planner such as Aive's queen search the directory directly. Read-only, never runs an agent, off until `MCP_ENABLED` is set.

The legacy `POST /api/v1/search` dispatch route now returns **410 Gone**. Existing result UUIDs remain readable until their seven-day retention expires. The separate `/api/v1/directory?q=...` semantic API remains available for clients such as Aive. It ranks candidates from the persistent curated registry with optional Workers AI; it also never triggers a crawl.

See [API](docs/API.md), [Architecture](docs/ARCHITECTURE.md), and [Operations](docs/OPERATIONS.md) for protocol, security, and deployment details.

## Who is behind an agent, and does it need a key?

Each listing says what its card **declares**, nothing more:

- **Source** (where the agent gets its thinking): the card's own `provider` name, with a flag when that website is not the website serving the agent. SearchA2A does not guess models from descriptions.
- **Host** (where it runs): inferred only from the endpoint's website name, for example `vercel.app`.
- **Key**: `keyless` is `declared`, `no` or `unknown`. `declared` means the card names no key. It is not proof that none is needed, and it says nothing about price.
- **How far it was checked**: level 0 (card read), 1 (card and address on the same website), 2 (a server answered at the address in the last week). Levels expire, and no one has tested what any agent does. See [API](docs/API.md#what-verification-levels-mean).

## Bring your own compute

Connect an owner-operated text agent through the optional **A2A 1.0 JSON-RPC adapter**, without forking this repository. Operators deploy the adapter to their own account with runtime configuration; GitHub Actions only tests and indexes. SearchA2A never executes agents or pays for their inference. See [Federated execution](docs/FEDERATION.md) for setup, security and limitations.
