# SearchA2A

Find AI agents by what they do. SearchA2A discovers publicly listed A2A Agent Cards, skills and endpoints without requiring you to register an account.

**[Search for agents](https://searcha2a.azphalt.store/)**

## Search

1. Enter a skill, capability or agent name, such as **weather**, **code review** or **translation**.
2. Select **Search indexed** for immediate matching against the published catalog. The SearchA2A Worker ranks the results with text embeddings when available; if inference fails, the page explicitly labels the keyword fallback. No Agent Card or agent task is contacted.
3. To discover new cards, complete the Cloudflare challenge and select **Search live**. This starts a GitHub Actions crawl and updates the results when it finishes.

A live search takes time to run. The page shows its progress and checks for results automatically. Until the new results arrive, cards from the recently indexed catalog may remain on screen. A search can also return no matches.

## Read the results

Each card shows an agent's description, advertised skills and discovery source. Select **Agent Card** to inspect its published metadata or **Endpoint** to open its advertised URL.

**Listings are not endorsements or availability checks.** An agent card can be outdated, inaccurate or unreachable. SearchA2A reads metadata; it does not run, install or authenticate the advertised agents.

## Browse without searching

- [Recently indexed agents](https://searcha2a.azphalt.store/api/v1/catalog.json): public JSON catalog, no verification needed.
- [Azphalt A2A Agents](https://azphalt.store/agents): browse discovered external agents in the Azphalt Store. These entries are informational, not installable Azphalt packages.

## API and other documentation

The [API reference](docs/API.md) includes the read-only **GET /api/v1/directory?q=...** matching endpoint for apps like Aive. Indexed matching needs no Turnstile; live discovery still requires verification. Workers AI inference is rate-limited and can use paid inference quota. Neither type of search authenticates Agent Card publishers.

[Operations](docs/OPERATIONS.md) covers setup and troubleshooting. [Architecture](docs/ARCHITECTURE.md) documents implementation and security boundaries. [Source configuration](sources.json) lists the curated discovery inputs, including a bounded paginated A2A Registry feed. The crawler fetches each listed agent's original public Agent Card; directory metadata alone is not treated as a verified card.
