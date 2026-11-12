# Federated execution: bring your own compute

SearchA2A remains a metadata-only, read-only discovery service. **The optional `federation/` Worker is an independent A2A endpoint that each fork owner deploys into their own Cloudflare account.** Search queries never invoke a Worker, dispatch GitHub Actions, or run agents. The central repository does not host, proxy or pay for agent execution.

## Fork and activate

1. Fork SearchA2A (or copy the `federation/` directory and the federation workflow to your own repository). GitHub disables scheduled workflows on forks by default; use **Actions** to enable workflows as needed. Forks remain independent of the upstream SearchA2A registry.
2. Edit [`federation/config.mjs`](../federation/config.mjs) with accurate public name, description, version and skills. These are declarations, **not inferred or verified capabilities**. Never put credentials in this file.
3. Provide a reachable owner-operated HTTPS webhook. It accepts `POST` JSON `{ "message": "...", "contextId": "...", "messageId": "..." }`, returning UTF-8 text or JSON `{ "text": "..." }` / `{ "response": "..." }`. The webhook runs inference and tools on **your** hardware, service or account. A local-only service needs an owner-operated outbound tunnel (for example Cloudflare Tunnel); this repository does not run a shared relay.
4. In your fork set **Actions variables** `FEDERATION_ENABLED=true` and `FEDERATION_PUBLIC_ORIGIN=https://<your-worker>.<your-subdomain>.workers.dev` (or your own custom Worker domain). Set **Actions secrets** `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `FEDERATION_UPSTREAM_URL`, `FEDERATION_GATEWAY_API_KEY` (random, at least 24 characters), and optionally `FEDERATION_UPSTREAM_BEARER_TOKEN`. The GitHub deployment credentials must belong to your account, not SearchA2A's central deployment.
5. Manually run **Federated A2A gateway (fork-owned)**. Future changes to `federation/` on main redeploy that fork's Worker. Public metadata is immediately generated at `https://<your-worker>/.well-known/agent-card.json`; the JSON-RPC endpoint is `/a2a`. A test job runs even when deployment is disabled. Do **not** enable federation for the central SearchA2A deployment.
6. To be found through the central registry, propose your public card URL for the curated [`sources.json`](../sources.json) seeds (or rely on an existing compatible registry). Discovery is independent of execution. A published listing does not prove ownership or availability. There is intentionally **no** public auto-registration write token.

### Example A2A request

```sh
curl -sS -X POST 'https://YOUR-WORKER.workers.dev/a2a' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_CLIENT_GATEWAY_KEY' \
  --data '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"role":"ROLE_USER","messageId":"hello-1","parts":[{"text":"Hello"}]}}}'
```

The response contains `result.message` with `ROLE_AGENT`, a new message ID, a context ID, and a text part. Give clients the gateway bearer key only through a secure channel. This is a shared key for the forked gateway, not SearchA2A account authentication.

## Supported scope and non-goals

- **A2A 1.0 JSON-RPC `SendMessage`**, synchronous text only. The Worker emits a v1 Agent Card with `streaming: false`, `pushNotifications: false` and declared bearer authentication. No claims of Tasks, task cancellation, push, MCP translation, streaming or automatic local execution.
- The upstream address is **operator-configured**, never chosen from client input or discovery results. HTTPS is mandatory; credentials, obvious local/private IP literals and local hostnames are rejected. Redirects are not followed. Network-level DNS rebinding protection is not provided; owners must trust the hostname they configure.
- Requests require the fork-owned bearer token; no permissive CORS, client-controlled target URLs or proxying of request Authorization to the upstream. A separate upstream token may be configured. Request bodies are capped at 16 KiB, upstream responses at 64 KiB, and upstream work times out after 15 seconds. Remote agent failure returns a safe error without leaking upstream response text.
- Cloudflare billing/usage, model inference, token sharing and webhook security belong to the fork operator. Free tiers and quota behavior can change. **This is not an assurance of zero operating cost.** To prevent paid usage, configure account-level limits and verify provider terms before activation.
- If the upstream is already a working A2A server, publish its own Agent Card instead. This adapter is for **non-A2A synchronous HTTP webhook agents**. Other agent protocols require an owner-operated adapter; a card alone cannot make an unreachable agent callable.
- The workflow removes the optional upstream bearer secret when its corresponding Actions secret is blank. Rotate the gateway key if it is leaked. Never expose Worker tokens in commit history, PRs, Pages, or registry cards.

## Rationale and invariants

The original SearchA2A per-search GitHub Action dispatch was removed because runner startup, callback polling and workflow contention made searches slow and expensive. Execution therefore must not reintroduce that path. SearchA2A's registry curation credentials must also remain separate from discovery ingestion and execution secrets: a discovered card remains untrusted metadata, never authority to invoke an agent.

Enforced by [`worker/index.mjs`](../worker/index.mjs) (`/api/v1/search` returns 410 and registry search is GET-only); [`federation/index.mjs`](../federation/index.mjs) (fixed owner endpoint, credential gate, bounded inputs, explicit supported method); and [`federation.yml`](../.github/workflows/federation.yml) (deployment gated behind `FEDERATION_ENABLED` and fork-scoped secrets). Tests: `node --test tests/federation.test.mjs`. Existing registry tests remain unchanged.
