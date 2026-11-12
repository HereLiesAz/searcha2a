# SearchA2A

Find AI agents by what they do. SearchA2A discovers publicly listed A2A Agent Cards, skills and endpoints without requiring you to register an account.

**[Search for agents](https://searcha2a.azphalt.store/)**

## Search

1. Enter a skill, capability or agent name, such as **weather**, **code review** or **translation**.
2. Complete the Cloudflare verification when prompted and select **Search agents**.
3. Matching agents from the latest published registry catalog appear immediately. The page shows **More results are coming when GitHub Actions finishes** while a fresh discovery run completes. Newly found agents are added automatically without removing the initial matches.

The indexed matches are a cached snapshot, not a live query of the entire registry. GitHub discovery may add agents or return no further matches. If Turnstile verification is incomplete, indexed matches still appear, but no GitHub workflow is dispatched until verification succeeds.

## Read the results

Each card shows an agent's description, advertised skills and discovery source. Select **Agent Card** to inspect its published metadata or **Endpoint** to open its advertised URL.

**Listings are not endorsements or availability checks.** An agent card can be outdated, inaccurate or unreachable. SearchA2A reads metadata; it does not run, install or authenticate the advertised agents.

## Browse without searching

- [Recently indexed agents](https://searcha2a.azphalt.store/api/v1/catalog.json): public JSON catalog, no verification needed.
- [Azphalt A2A Agents](https://azphalt.store/agents): browse discovered external agents in the Azphalt Store. These entries are informational, not installable Azphalt packages.

## API and other documentation

The [API reference](docs/API.md) explains how to read public data and how the live search API works. The live API requires a valid Cloudflare verification token; the static catalog does not.

[Operations](docs/OPERATIONS.md) covers setup and troubleshooting. [Architecture](docs/ARCHITECTURE.md) documents implementation and security boundaries. [Source configuration](sources.json) lists the curated discovery inputs, including a bounded paginated A2A Registry feed. The crawler fetches each listed agent's original public Agent Card; directory metadata alone is not treated as a verified card.
