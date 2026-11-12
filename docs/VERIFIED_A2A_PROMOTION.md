# Signed Azphalt listings from SearchA2A Agent Cards

Status: source generation implemented; package signing remains in Azphalt's existing reviewed registry build.

## Why this is a two-stage trust chain

Discovery does not establish trust. A public Agent Card can be forged or changed, and
an HTTPS connection proves only control of its TLS hostname at the time fetched.
A **signed** Azphalt `.azp` establishes the integrity and Azphalt packager key of
the *connection description*, not the agent's legal identity, availability, or
permission to run tasks.

An automated proposal requires these independently checked conditions:

1. SearchA2A fetches a public HTTPS Agent Card through its bounded, no-redirect,
   public-DNS resolver. The normalized endpoint must be on the card's origin.
2. The card has `"azphalt": {"externalAgentListing": true}` and advertises
   JSON-RPC over `supportedInterfaces`. No alternate A2A protocols are silently
   translated into JSON-RPC.
3. The same HTTPS origin publishes
   `/.well-known/azphalt-a2a-verification.json` with consent, exact card and
   endpoint URLs, a MIT metadata licence, and an unexpired statement.
4. Azphalt's promotion job **re-fetches** both the original card and proof. A
   stale result-listing flag alone is insufficient. Unsupported authentication
   (e.g. OAuth flows without a supported bearer route) is not auto-promoted.
5. It generates a `kind: "llm"`, `endpoint.protocols: ["a2a-jsonrpc"]` package
   with `setup/setup.sh` and `setup/agent.json`. The shell script is **fixed**
   and shared across generated packages. All publisher input stays in signed JSON.
   The script validates the connection configuration, never runs publisher code,
   fetches a task endpoint, or stores credentials.
6. Azphalt's existing `@azphalt/submit-check` / `verifyAzp` and security sweep
   validate the generated manifest and files before opening a source PR. A
   maintainer reviews the source. After merge, registry-sync builds and signs
   the `.azp` using `AZPHALT_PACKAGE_SIGNING_KEY`, then opens the existing
   separate packaged-bytes PR for review. **A2A promotion refuses unsigned
   publication.** This process never stores the signing key in SearchA2A.

Example opt-in extension on an Agent Card:

```json
{
  "name": "Forecast Agent",
  "url": "https://forecast.example.org/a2a",
  "supportedInterfaces": [
    {"url": "https://forecast.example.org/a2a", "protocolBinding": "JSONRPC"}
  ],
  "skills": [{"id": "forecast", "name": "Forecast", "description": "Weather"}],
  "azphalt": {"externalAgentListing": true}
}
```

Example publisher proof at the **same origin**:

```json
{
  "schema": 1,
  "listing": "azphalt-external-a2a",
  "consent": true,
  "license": "MIT",
  "card_url": "https://forecast.example.org/.well-known/agent-card.json",
  "endpoint": "https://forecast.example.org/a2a",
  "issued_at": "2026-11-12T10:00:00+00:00",
  "expires_at": "2026-11-15T10:00:00+00:00"
}
```

Proofs older than seven days and expired proofs fail. Automatic generated
evidence is capped at 24 hours; the next crawl must revalidate it. Losing the
proof does **not** silently delete a previously published package, since
transient outages cannot be equated with revocation. New connections must still
re-fetch and revalidate the Agent Card at runtime. Revocations use Azphalt's
existing reviewed revocation mechanism.

## Coverage and limits

Scheduled crawling reads up to **five pages of 30** from each configured
paginated registry (max two registries), with **210 distinct candidate URLs**
and a reserve of up to **50 GitHub-derived leads**. Each scheduled snapshot
publishes up to **150 normalized Agent Cards**. Queries deliberately keep
shorter live-source retrieval; the persistent SQL registry retains previously
discovered cards across crawls and may hold more than one snapshot.

The promotion job examines up to 150 feed listings, generates at most 12 new
submissions per run, and revalidates previous automated submissions. It never
overwrites unrelated submitter files, follows symlinks, executes agent-provided
scripts, or acquires an A2A token. If verification fails, the entry remains an
untrusted discovery listing. A `publisher_verification` field in the public
feed is only a **lead**, not a trusted package or an automatic authorization.

## Automation

Azphalt runs `.github/workflows/promote-searcha2a.yml` daily (04:47 UTC) and
on manual dispatch. It checks out SearchA2A, runs verification tests, generates
deterministic source folders under `submissions/org.azphalt.a2a.*`, executes
the generated setup checks in a sandbox, validates through Azphalt's actual
package verifier, and opens a PR.

It requires `AZPHALT_PACKAGE_SIGNING_KEY` to be configured in the Azphalt repo
before proposing signed releases. A PAT in `AZPHALT_AUTOMATION_TOKEN` with
appropriate repository permissions may be used to trigger PR workflows; a
default `GITHUB_TOKEN` can create the PR but does not cause follow-on
`pull_request` workflows to run. GitHub repo Actions settings must allow PR
creation. Approval and package publication retain Azphalt's established review
and signing pipeline.

**No signing key or package-signing authority is delegated to SearchA2A.**
