/** SearchA2A gateway: protected live discovery and read-only indexed capability matching. */
import { searchIndexedAgents } from './semantic.mjs';
const REPOSITORY = 'HereLiesAz/searcha2a';
const JSON_HEADERS = { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function respond(body, status = 200, origin = '', allowedOrigin = '') {
  const headers = new Headers(JSON_HEADERS);
  if (origin && origin === allowedOrigin) {
    headers.set('Access-Control-Allow-Origin', allowedOrigin);
    headers.set('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    headers.set('Access-Control-Allow-Headers', 'Content-Type');
    headers.set('Vary', 'Origin');
  }
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers });
}

async function verifyTurnstile(token, ip, secret) {
  if (!token || !secret) return false;
  const params = new URLSearchParams({ secret, response: token });
  if (ip) params.set('remoteip', ip);
  const check = await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify', {
    method: 'POST', body: params, signal: AbortSignal.timeout(7000),
  });
  if (!check.ok) return false;
  const data = await check.json();
  return data.success === true;
}


/** A strongly consistent SQLite-backed mailbox per random search UUID. */
export class SearchResult {
  constructor(ctx) { this.ctx = ctx; }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    const store = this.ctx.storage.kv;

    if (request.method === 'PUT' && path === '/create') {
      const { id, query } = await request.json();
      if (store.get('result')) return respond({ error: 'Search already exists' }, 409);
      const expires_at = Date.now() + 604800000; // Same seven-day retention as the old KV mailbox.
      await this.ctx.storage.setAlarm(expires_at);
      store.put('result', { id, query, status: 'queued', expires_at });
      return respond({ status: 'queued' }, 201);
    }

    const saved = store.get('result');
    if (!saved || saved.expires_at <= Date.now()) {
      if (saved) store.delete('result');
      return respond({ error: 'Search expired or unknown' }, 404);
    }
    if (request.method === 'GET' && path === '/result') {
      const { expires_at, ...publicResult } = saved;
      return respond(publicResult);
    }
    if (request.method === 'DELETE' && path === '/result') {
      await this.ctx.storage.deleteAll();
      return respond({ status: 'deleted' });
    }
    if (request.method === 'PUT' && path === '/dispatch') {
      const run = await request.json();
      const current = store.get('result');
      // A callback can complete before GitHub's dispatch response arrives.
      if (current.status === 'queued') {
        store.put('result', {
          ...current,
          workflow_run_id: run.workflow_run_id || null,
          workflow_url: run.workflow_url || null,
        });
      }
      return respond({ status: current.status });
    }
    if (request.method === 'POST' && path === '/complete') {
      const data = await request.json();
      const current = store.get('result');
      if (current.status !== 'queued' || current.id !== data.request_id ||
          current.query !== data.query || !Array.isArray(data.results)) {
        return respond({ error: 'Result mismatches request' }, 409);
      }
      store.put('result', {
        ...current, status: 'complete', results: data.results.slice(0, 70),
        count: Math.min(data.results.length, 70),
        warnings: Array.isArray(data.warnings) ? data.warnings.slice(0, 10) : [],
        generated_at: data.generated_at || new Date().toISOString(),
      });
      return respond({ status: 'stored' });
    }
    return respond({ error: 'Not found' }, 404);
  }

  async alarm() { await this.ctx.storage.deleteAll(); }
}

function resultStub(env, id) {
  return env.RESULTS.get(env.RESULTS.idFromName(id));
}

async function handle(request, env) {
  const url = new URL(request.url);
  const origin = request.headers.get('Origin') || '';
  const allowedOrigin = env.ALLOWED_ORIGIN || '';
  const reply = (body, status) => respond(body, status, origin, allowedOrigin);
  // This public GET API searches only the indexed metadata snapshot. No GitHub workflow,
  // third-party Agent Card, credential or agent task is contacted.
  if (url.pathname === '/api/v1/directory') {
    const directoryReply = (body, status = 200) => new Response(
      status === 204 ? null : JSON.stringify(body),
      { status, headers: {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, OPTIONS',
        'Vary': 'Origin',
      } },
    );
    if (request.method === 'OPTIONS') return directoryReply({}, 204);
    if (request.method !== 'GET') return directoryReply({ error: 'Method not allowed' }, 405);
    const query = (url.searchParams.get('q') || '').trim().replace(/\s+/g, ' ');
    if (query.length < 2 || query.length > 120 || /[\x00-\x1f]/.test(query)) {
      return directoryReply({ error: 'Query must be 2–120 characters' }, 400);
    }
    if (!env.SEARCH_RATE_LIMITER) return directoryReply({ error: 'Search unavailable' }, 503);
    try {
      const limit = await env.SEARCH_RATE_LIMITER.limit({
        key: 'directory:' + (request.headers.get('CF-Connecting-IP') || 'unknown'),
      });
      if (!limit.success) return directoryReply({ error: 'Search rate limit exceeded' }, 429);
    } catch { return directoryReply({ error: 'Search unavailable' }, 503); }
    try {
      return directoryReply(await searchIndexedAgents(query, env));
    } catch { return directoryReply({ error: 'Published directory temporarily unavailable' }, 502); }
  }
  if (request.method === 'OPTIONS') {
    return origin === allowedOrigin ? reply({}, 204) : reply({ error: 'Origin not allowed' }, 403);
  }
  // A browser origin check is not authentication. Turnstile and rate limiting
  // below defend the publicly accessible dispatch operation.
  if (request.method === 'POST' && url.pathname === '/api/v1/search') {
    if (origin !== allowedOrigin) return reply({ error: 'Origin not allowed' }, 403);
    if (!env.GITHUB_DISPATCH_TOKEN || !env.TURNSTILE_SECRET || !env.RESULTS || !env.SEARCH_RATE_LIMITER) {
      return reply({ error: 'Gateway not configured' }, 503);
    }
    let input;
    try { input = await request.json(); } catch { return reply({ error: 'Invalid JSON' }, 400); }
    const query = typeof input.query === 'string' ? input.query.trim().replace(/\s+/g, ' ') : '';
    if (query.length < 2 || query.length > 120 || /[\x00-\x1f]/.test(query)) {
      return reply({ error: 'Query must be 2–120 characters' }, 400);
    }
    const turnstile = await verifyTurnstile(input.turnstile, request.headers.get('CF-Connecting-IP'), env.TURNSTILE_SECRET);
    if (!turnstile) return reply({ error: 'Challenge verification failed' }, 403);
    const limit = await env.SEARCH_RATE_LIMITER.limit({ key: request.headers.get('CF-Connecting-IP') || 'unknown' });
    if (!limit.success) return reply({ error: 'Search rate limit exceeded' }, 429);
    const id = crypto.randomUUID();
    const stub = resultStub(env, id);
    const created = await stub.fetch(new Request('https://results.internal/create', {
      method: 'PUT', body: JSON.stringify({ id, query }),
    }));
    if (!created.ok) return reply({ error: 'Could not create search request' }, 503);
    let response;
    try {
      response = await fetch(`https://api.github.com/repos/${REPOSITORY}/actions/workflows/search.yml/dispatches`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': 'A2search-worker',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ ref: 'main', inputs: { query, request_id: id } }),
        signal: AbortSignal.timeout(10000),
      });
    } catch {
      await stub.fetch(new Request('https://results.internal/result', { method: 'DELETE' }));
      return reply({ error: 'Could not reach GitHub Actions' }, 502);
    }
    if (!response.ok) {
      await stub.fetch(new Request('https://results.internal/result', { method: 'DELETE' }));
      return reply({ error: `GitHub Actions rejected dispatch (${response.status})` }, 502);
    }
    let run = {};
    try { run = await response.json(); } catch { /* Legacy 204 response. */ }
    const queued = { id, query, status: 'queued', workflow_run_id: run.workflow_run_id || null,
      workflow_url: run.html_url || null };
    await stub.fetch(new Request('https://results.internal/dispatch', {
      method: 'PUT', body: JSON.stringify(queued),
    }));
    return reply(queued, 202);
  }
  const idMatch = url.pathname.match(/^\/api\/v1\/results\/([^/]+)$/);
  if (request.method === 'GET' && idMatch) {
    if (!UUID.test(idMatch[1])) return reply({ error: 'Invalid search ID' }, 400);
    const saved = await resultStub(env, idMatch[1]).fetch(new Request('https://results.internal/result'));
    return reply(await saved.json(), saved.status);
  }
  if (request.method === 'POST' && url.pathname === '/internal/results') {
    if (!env.RESULT_CALLBACK_SECRET || request.headers.get('Authorization') !== `Bearer ${env.RESULT_CALLBACK_SECRET}`) {
      return reply({ error: 'Unauthorized' }, 401);
    }
    const bodyText = await request.text();
    if (bodyText.length > 700000) return reply({ error: 'Result too large' }, 413);
    let data;
    try { data = JSON.parse(bodyText); } catch { return reply({ error: 'Invalid JSON' }, 400); }
    if (!UUID.test(data.request_id || '')) return reply({ error: 'Bad request ID' }, 400);
    const saved = await resultStub(env, data.request_id).fetch(new Request('https://results.internal/complete', {
      method: 'POST', body: bodyText,
    }));
    return reply(await saved.json(), saved.status);
  }
  return reply({ error: 'Not found' }, 404);
}

export { handle };
export default { async fetch(request, env) {
  try { return await handle(request, env); }
  catch { return respond({ error: 'Internal error' }, 500, request.headers.get('Origin') || '', env.ALLOWED_ORIGIN || ''); }
} };
