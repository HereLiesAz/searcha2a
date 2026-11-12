# Federated execution: bring your own compute

SearchA2A is a shared discovery and connection directory, **not a shared agent execution host**. Its optional `federation/` adapter can be deployed straight from the SearchA2A source to the **agent operator's own Cloudflare account**, without creating a fork or GitHub repository. Execution and inference remain with each agent owner. Existing A2A servers need no adapter: publish their native Agent Cards.

## Connect an agent without forking

1. Supply an operator-controlled **public HTTPS webhook**. It accepts `POST` JSON `{ "message": "...", "contextId": "...", "messageId": "..." }` and returns UTF-8 text or JSON `{ "text": "..." }` / `{ "response": "..." }`. For a local-only agent, the operator must arrange their own outbound tunnel or hosting. SearchA2A does not run a relay.
2. Download or clone [SearchA2A](https://github.com/HereLiesAz/searcha2a), then `cd federation`. No fork, GitHub Actions setup, or code edits are required. Log in with `npx wrangler@4 login` (or use your own Cloudflare API token).
3. Deploy the adapter into **your** account, supplying public metadata as runtime variables. Replace the example Worker URL with the hostname assigned to your Worker, and set your own unique Worker name:

```sh
npx wrangler@4 deploy --name my-agent-a2a \
  --var 'PUBLIC_ORIGIN:https://my-agent-a2a.YOUR_SUBDOMAIN.workers.dev' \
  --var 'AGENT_NAME:My Agent' \
  --var 'AGENT_DESCRIPTION:An agent running on my infrastructure' \
  --var 'AGENT_VERSION:1.0.0' \
  --var 'AGENT_SKILLS_JSON:[{"id":"text-chat","name":"Text conversation","description":"Replies to text","tags":["text"]}]'
```

4. Install secrets interactively against **the same Worker** (do not put them in GitHub, source code, or public metadata):

```sh
npx wrangler@4 secret put UPSTREAM_URL --name my-agent-a2a
npx wrangler@4 secret put GATEWAY_API_KEY --name my-agent-a2a
# Optional, if your webhook requires bearer authentication:
npx wrangler@4 secret put UPSTREAM_BEARER_TOKEN --name my-agent-a2a
```

`UPSTREAM_URL` must be a public HTTPS URL for the operator's webhook. `GATEWAY_API_KEY` must be at least 24 random characters and must be shared with authorized A2A clients out of band. The Worker is unavailable until its metadata and mandatory secrets are set. In a multi-Worker account, choose a distinct `--name` for each adapter and use it consistently.

5. Check the published card at `https://my-agent-a2a.YOUR_SUBDOMAIN.workers.dev/.well-known/agent-card.json`. Its supported interface points to `/a2a` on that same Worker. Clients call the Worker directly; SearchA2A never handles their task bodies or secrets.
6. To enter the SearchA2A directory, submit the public card URL for inclusion in the curated [`sources.json`](../sources.json) or publish it through an indexed A2A registry. SearchA2A indexes metadata only, and a listed card is not proof of identity, authorization or runtime health. There is currently no unauthenticated auto-registration endpoint.

### Example A2A request

```sh
curl -sS -X POST 'https://my-agent-a2a.YOUR_SUBDOMAIN.workers.dev/a2a' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_CLIENT_GATEWAY_KEY' \
  --data '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"role":"ROLE_USER","messageId":"hello-1","parts":[{"text":"Hello"}]}}}'
```

The response contains `result.message`, `ROLE_AGENT`, a new message ID, a context ID and a text part. The gateway bearer key is an operator-owned secret, not a SearchA2A account credential.

## Supported scope and limitations

- **A2A 1.0 JSON-RPC `SendMessage`, synchronous text only.** Advertised capabilities intentionally set `streaming: false` and `pushNotifications: false`; tasks, task cancellation, push, streaming and MCP translation are not implemented.
- The upstream is **operator-configured** and never supplied by clients or discovery results. HTTPS is mandatory; local/private IP literals and local hostnames are rejected, and redirects are not followed. DNS rebinding protections are not provided; only configure trusted hostnames.
- Each request requires the operator's bearer key, with limits of 16 KiB input, 64 KiB upstream response and 15 seconds upstream processing. The caller's authorization header is not forwarded to the upstream. Errors do not disclose upstream response contents.
- Cloudflare quota, webhook reliability, inference expenses and credential distribution belong to **each agent operator**. Free tiers can change and no one can guarantee zero charges; set hard account quotas where offered.
- **Do not deploy this adapter if the agent already speaks A2A.** Publish that existing agent's card instead.
- The central SearchA2A Actions remain indexing and tests only. They do not deploy agent adapters or invoke advertised endpoints. Optional adapter tests run in `.github/workflows/federation.yml`; deployment is deliberately external to the repository.

## Architecture and invariants

The original per-search GitHub Actions dispatch was removed because startup delays, callback polling and workflow contention made searches slow. The existing registry therefore remains immediate, read-only, and separate from execution. Discovery credentials cannot authorize execution, and a crawled card cannot select or trigger an upstream endpoint.

Enforcement: [`worker/index.mjs`](../worker/index.mjs) keeps `/api/v1/search` retired and registry searches read-only; [`federation/index.mjs`](../federation/index.mjs) requires configured owner credentials, fixed HTTPS upstream, bounded bodies and explicit supported methods; [`.github/workflows/federation.yml`](../.github/workflows/federation.yml) performs tests only. Regression tests: `node --test tests/federation.test.mjs`.