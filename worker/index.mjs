/** SearchA2A registry API and legacy result mailbox. Crawls run only on a schedule. */
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



/**
 * Durable, searchable index fed only by the scheduled crawler.
 * A failed or empty crawl never erases previously indexed agent records.
 */
export class AgentRegistry {
  constructor(ctx) {
    this.ctx = ctx;
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS agents (
      id TEXT PRIMARY KEY,
      name_search TEXT NOT NULL,
      skills_search TEXT NOT NULL,
      description_search TEXT NOT NULL,
      search_text TEXT NOT NULL,
      record TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )`);
    this.sql.exec('CREATE TABLE IF NOT EXISTS registry_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)');
  }

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    if (request.method === 'POST' && path === '/ingest') {
      let data;
      try { data = await request.json(); } catch { return respond({ error: 'Invalid JSON' }, 400); }
      if (!data || data.query !== '' || !Array.isArray(data.results) || data.results.length > 70) {
        return respond({ error: 'Expected a bounded scheduled crawl' }, 400);
      }
      // Validate the entire batch before writing any rows.
      const entries = data.results;
      if (entries.some(a => !a || typeof a !== 'object'
            || !/^[0-9a-f]{20}$/.test(a.id || '')
            || typeof a.name !== 'string' || !a.name.trim() || a.name.length > 160
            || typeof a.description !== 'string'
            || typeof a.source_url !== 'string' || !a.source_url.startsWith('https://')
            || !Array.isArray(a.skills) || !a.skills.length
            || a.verification !== 'card_read_only')) {
        return respond({ error: 'Invalid Agent Card record' }, 400);
      }
      const updatedAt = new Date().toISOString();
      this.ctx.storage.transactionSync(() => {
        for (const agent of entries) {
          const name = agent.name.toLowerCase();
          const description = agent.description.toLowerCase();
          const skills = agent.skills.map(skill => [
            skill && typeof skill.name === 'string' ? skill.name : '',
            skill && typeof skill.description === 'string' ? skill.description : '',
            ...(skill && Array.isArray(skill.tags) ? skill.tags.filter(tag => typeof tag === 'string') : []),
          ].join(' ')).join(' ').toLowerCase();
          const search = [name, description, skills, agent.endpoint || ''].join(' ').toLowerCase();
          this.sql.exec(`INSERT INTO agents
            (id, name_search, skills_search, description_search, search_text, record, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name_search=excluded.name_search, skills_search=excluded.skills_search,
              description_search=excluded.description_search, search_text=excluded.search_text,
              record=excluded.record, updated_at=excluded.updated_at`,
              agent.id, name, skills, description, search, JSON.stringify(agent), updatedAt);
        }
        this.sql.exec(`INSERT INTO registry_meta (key, value) VALUES ('last_ingested_at', ?)
          ON CONFLICT(key) DO UPDATE SET value=excluded.value`, updatedAt);
      });
      const count = this.sql.exec('SELECT COUNT(*) AS total FROM agents').one().total;
      return respond({ status: 'stored', ingested: entries.length, total: count });
    }

    if (request.method === 'GET' && path === '/search') {
      const query = (url.searchParams.get('query') || '').trim().replace(/\s+/g, ' ');
      if (query.length > 120 || /[\x00-\x1f]/.test(query)) return respond({ error: 'Invalid query' }, 400);
      const limitText = url.searchParams.get('limit') || '50';
      const offsetText = url.searchParams.get('offset') || '0';
      if (!/^\d{1,3}$/.test(limitText) || !/^\d{1,6}$/.test(offsetText)) {
        return respond({ error: 'Invalid pagination' }, 400);
      }
      const limit = Number(limitText), offset = Number(offsetText);
      if (limit < 1 || limit > 100 || offset > 100000) return respond({ error: 'Invalid pagination' }, 400);

      const terms = (query.toLowerCase().match(/[\p{L}\p{N}-]+/gu) || []).slice(0, 12);
      const filters = terms.map(() => 'search_text LIKE ?').join(' OR ');
      const where = filters ? 'WHERE ' + filters : '';
      const bindings = terms.map(term => '%' + term + '%');
      const matched = this.sql.exec(`SELECT COUNT(*) AS total FROM agents ${where}`, ...bindings).one().total;
      const total = this.sql.exec('SELECT COUNT(*) AS total FROM agents').one().total;
      const relevance = terms.map(() =>
        '(CASE WHEN name_search LIKE ? THEN 8 ELSE 0 END + ' +
        'CASE WHEN skills_search LIKE ? THEN 4 ELSE 0 END + ' +
        'CASE WHEN description_search LIKE ? THEN 1 ELSE 0 END)').join(' + ');
      const sort = relevance ? relevance + ' DESC, name_search ASC' : 'name_search ASC';
      const rankParams = terms.flatMap(term => ['%' + term + '%', '%' + term + '%', '%' + term + '%']);
      const rows = this.sql.exec(
        `SELECT record FROM agents ${where} ORDER BY ${sort} LIMIT ? OFFSET ?`,
        ...bindings, ...rankParams, limit, offset).toArray();
      const meta = this.sql.exec(
        "SELECT value FROM registry_meta WHERE key='last_ingested_at'").one();
      return respond({
        query, count: matched, total, offset, limit,
        generated_at: meta ? meta.value : null,
        results: rows.map(row => JSON.parse(row.record)),
        verified: 'Agent Card metadata only; endpoints are not actively probed',
      });
    }

    const idMatch = path.match(/^\/agent\/([0-9a-f]{20})$/);
    if (request.method === 'GET' && idMatch) {
      const record = this.sql.exec('SELECT record FROM agents WHERE id=?', idMatch[1]).one();
      return record ? respond(JSON.parse(record.record)) : respond({ error: 'Agent unknown' }, 404);
    }
    return respond({ error: 'Not found' }, 404);
  }
}

function registryStub(env) {
  return env.REGISTRY.get(env.REGISTRY.idFromName('searcha2a-registry'));
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
  if (request.method === 'OPTIONS') {
    return origin === allowedOrigin ? reply({}, 204) : reply({ error: 'Origin not allowed' }, 403);
  }
  // Legacy POST dispatch is deliberately retired; all interactive searches are registry reads.
  if (request.method === 'POST' && url.pathname === '/api/v1/search') {
    return reply({ error: 'Per-query crawling retired. Use GET /api/v1/agents?query=...' }, 410);
  }
  if (request.method === 'GET' &&
      (url.pathname === '/api/v1/agents' || url.pathname.startsWith('/api/v1/agents/'))) {
    if (!env.REGISTRY) return reply({ error: 'Registry not configured' }, 503);
    const path = url.pathname === '/api/v1/agents'
      ? '/search' + url.search
      : '/agent/' + url.pathname.slice('/api/v1/agents/'.length);
    const result = await registryStub(env).fetch(new Request('https://registry.internal' + path));
    return reply(await result.json(), result.status);
  }
  if (request.method === 'POST' && url.pathname === '/internal/registry/ingest') {
    // Reuse the already-provisioned shared Actions/Worker secret. No browser writes.
    if (!env.RESULT_CALLBACK_SECRET ||
        request.headers.get('Authorization') !== `Bearer ${env.RESULT_CALLBACK_SECRET}`) {
      return reply({ error: 'Unauthorized' }, 401);
    }
    if (!env.REGISTRY) return reply({ error: 'Registry not configured' }, 503);
    const body = await request.text();
    if (body.length > 1000000) return reply({ error: 'Batch too large' }, 413);
    const result = await registryStub(env).fetch(new Request('https://registry.internal/ingest', {
      method: 'POST', body,
    }));
    return reply(await result.json(), result.status);
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
