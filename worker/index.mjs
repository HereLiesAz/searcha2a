/** Scheduled registry, unified semantic ANS search, and legacy result mailboxes. */
import { rankAgentCandidates, descriptionOf, EMBEDDING_MODEL, validEmbeddings } from './semantic.mjs';
import { normalizeAnsName, parseAnsClaim } from './ans.mjs';
import {
  validFacets, HEALTH_STATE_STATUS, HEALTH_STATUSES, CHALLENGES, stateFromHttp,
} from './facets.mjs';
import { handleMcp } from './mcp.mjs';
import { parseFilters, matchesFilters, sortEntries, MAX_FILTER_SCAN } from './search.mjs';
import { computeVerification, verificationDetail, effectiveAccess } from './verification.mjs';
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

/**
 * Durable, searchable index fed only by the scheduled crawler.
 * A failed or empty crawl never erases previously indexed agent records.
 */
export class AgentRegistry {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.ai = env?.AI;
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
    // Add governance columns to the already-deployed v2 registry without erasing records.
    const columns = new Set(this.sql.exec('PRAGMA table_info(agents)').toArray().map(row => row.name));
    if (!columns.has('listing_state')) {
      this.sql.exec("ALTER TABLE agents ADD COLUMN listing_state TEXT NOT NULL DEFAULT 'active'");
    }
    if (!columns.has('review_status')) {
      this.sql.exec("ALTER TABLE agents ADD COLUMN review_status TEXT NOT NULL DEFAULT 'unreviewed'");
    }
    if (!columns.has('ans_name')) {
      this.sql.exec('ALTER TABLE agents ADD COLUMN ans_name TEXT');
    }
    this.sql.exec('CREATE INDEX IF NOT EXISTS idx_agents_ans_name ON agents(ans_name)');
    // Store vectors separately from the public Agent Card and moderation state.
    // The model and exact embedding input form the cache version.
    this.sql.exec(`CREATE TABLE IF NOT EXISTS agent_embeddings (
      id TEXT PRIMARY KEY,
      model TEXT NOT NULL,
      input_text TEXT NOT NULL,
      vector TEXT NOT NULL
    )`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS agent_health (
      id TEXT PRIMARY KEY,
      endpoint TEXT NOT NULL,
      status TEXT NOT NULL,
      http_status INTEGER,
      checked_at TEXT NOT NULL
    )`);
    // Additive honest-taxonomy columns; the legacy status/http_status columns keep their meaning.
    const healthColumns = new Set(this.sql.exec('PRAGMA table_info(agent_health)').toArray()
      .map(row => row.name));
    for (const [column, type] of [['state', 'TEXT'], ['redirect', 'INTEGER'],
        ['challenge', 'TEXT'], ['tls_not_after', 'TEXT']]) {
      if (!healthColumns.has(column)) this.sql.exec(`ALTER TABLE agent_health ADD COLUMN ${column} ${type}`);
    }
    this.sql.exec(`CREATE TABLE IF NOT EXISTS registry_audit (
      revision INTEGER PRIMARY KEY AUTOINCREMENT,
      agent_id TEXT NOT NULL,
      action TEXT NOT NULL,
      listing_state TEXT NOT NULL,
      review_status TEXT NOT NULL,
      reason TEXT NOT NULL,
      changed_at TEXT NOT NULL
    )`);
  }

  async indexMissingEmbeddings(candidates) {
    if (!this.ai || typeof this.ai.run !== 'function' || !candidates.length) return new Map();
    const stored = new Map();
    try {
      // The Workers AI model accepts up to 100 input strings per request.
      for (let start = 0; start < candidates.length; start += 100) {
        const batch = candidates.slice(start, start + 100);
        const response = await this.ai.run(EMBEDDING_MODEL, {
          text: batch.map(item => item.text), pooling: 'cls',
        });
        if (!validEmbeddings(response?.data, batch.length)) return stored;
        this.ctx.storage.transactionSync(() => {
          batch.forEach((item, i) => {
            // A concurrent crawl may have changed the record while inference ran.
            // Never persist a vector against stale or moderated metadata.
            const latest = this.sql.exec(`SELECT record FROM agents WHERE id=?
              AND listing_state='active' AND review_status != 'rejected'`, item.id).toArray()[0];
            if (!latest || descriptionOf(JSON.parse(latest.record)) !== item.text) return;
            this.sql.exec(`INSERT INTO agent_embeddings (id, model, input_text, vector)
              VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
              model=excluded.model, input_text=excluded.input_text, vector=excluded.vector`,
            item.id, EMBEDDING_MODEL, item.text, JSON.stringify(response.data[i]));
            stored.set(item.id, response.data[i]);
          });
        });
      }
    } catch {
      // A model outage must not corrupt a durable index or bypass moderation.
    }
    return stored;
  }

  /** Evidence rows for agents: health probes and the latest approval time (chunked for SQL limits). */
  verificationFor(rows, now) {
    const health = new Map();
    const approvals = new Map();
    for (let start = 0; start < rows.length; start += 80) {
      const ids = rows.slice(start, start + 80).map(row => row.id);
      const marks = ids.map(() => '?').join(', ');
      for (const item of this.sql.exec(`SELECT id, endpoint, status, http_status, checked_at,
        state, redirect, challenge, tls_not_after FROM agent_health WHERE id IN (${marks})`,
      ...ids).toArray()) health.set(item.id, item);
      for (const item of this.sql.exec(`SELECT agent_id, MAX(changed_at) AS approved_at
        FROM registry_audit WHERE action='approve' AND agent_id IN (${marks}) GROUP BY agent_id`,
      ...ids).toArray()) approvals.set(item.agent_id, item.approved_at);
    }
    return rows.map(row => {
      const record = JSON.parse(row.record);
      const evidence = health.get(row.id) || null;
      return {
        record, health: evidence,
        full: computeVerification({
          record, review_status: row.review_status, updated_at: row.updated_at,
          health: evidence, approved_at: approvals.get(row.id) || null,
        }, now),
      };
    });
  }

  /** Public form of stored rows, paired with the full evidence they were computed from. */
  presentEntries(rows) {
    const now = new Date();
    return this.verificationFor(rows, now).map(({ record, health, full }, i) => ({
      full,
      item: {
        ...record, curation_status: rows[i].review_status,
        verification_detail: verificationDetail(full),
        access: effectiveAccess(record, health, now),
      },
    }));
  }

  /** Public form of a stored row: the card record plus additive, read-time evidence. */
  present(rows) {
    return this.presentEntries(rows).map(entry => entry.item);
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
            || !Array.isArray(a.skills)
            || (a.skills.length === 0 && (a.schema_status !== 'partial_agent_card'
              || typeof a.endpoint !== 'string' || !a.endpoint.startsWith('https://')
              || !Array.isArray(a.protocols) || !a.protocols.length))
            || a.verification !== 'card_read_only'
            || !validFacets(a))) {
        return respond({ error: 'Invalid Agent Card record' }, 400);
      }
      const updatedAt = new Date().toISOString();
      const pendingEmbeddings = [];
      this.ctx.storage.transactionSync(() => {
        for (const agent of entries) {
          const name = agent.name.toLowerCase();
          const description = agent.description.toLowerCase();
          const skills = agent.skills.map(skill => [
            skill && typeof skill.name === 'string' ? skill.name : '',
            skill && typeof skill.description === 'string' ? skill.description : '',
            ...(skill && Array.isArray(skill.tags) ? skill.tags.filter(tag => typeof tag === 'string') : []),
          ].join(' ')).join(' ').toLowerCase();
          const claim = parseAnsClaim(agent.ans_name, agent.ans_host);
          const { ans, ans_name, ans_host, ...metadata } = agent;
          const record = { ...metadata, ...(claim ? { ans: claim } : {}) };
          const embeddingText = descriptionOf(record);
          const cached = this.sql.exec(
            'SELECT input_text, model FROM agent_embeddings WHERE id=?', agent.id).toArray()[0];
          if (!cached || cached.input_text !== embeddingText || cached.model !== EMBEDDING_MODEL) {
            this.sql.exec('DELETE FROM agent_embeddings WHERE id=?', agent.id);
            pendingEmbeddings.push({ id: agent.id, text: embeddingText });
          }
          const search = [name, description, skills, agent.endpoint || '', claim?.name || ''].join(' ').toLowerCase();
          this.sql.exec(`INSERT INTO agents
            (id, name_search, skills_search, description_search, search_text, record, updated_at, ans_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name_search=excluded.name_search, skills_search=excluded.skills_search,
              description_search=excluded.description_search, search_text=excluded.search_text,
              record=excluded.record, updated_at=excluded.updated_at, ans_name=excluded.ans_name`,
              agent.id, name, skills, description, search, JSON.stringify(record), updatedAt,
              claim ? normalizeAnsName(claim.name) : null);
        }
        if (entries.length) this.sql.exec(`INSERT INTO registry_meta (key, value) VALUES ('last_ingested_at', ?)
          ON CONFLICT(key) DO UPDATE SET value=excluded.value`, updatedAt);
      });
      // Background index updates are best-effort. Failed embeddings remain missing
      // and are retried by the next directory read without failing ingestion.
      if (pendingEmbeddings.length && this.ctx.waitUntil && this.ai) {
        // Never send suppressed records to the embedding model, even for
        // background work scheduled by a later crawl.
        const eligible = pendingEmbeddings.filter(item => this.sql.exec(
          `SELECT id FROM agents WHERE id=?
            AND listing_state='active' AND review_status != 'rejected'`, item.id
        ).toArray().length);
        if (eligible.length) this.ctx.waitUntil(this.indexMissingEmbeddings(eligible));
      }
      const count = this.sql.exec('SELECT COUNT(*) AS total FROM agents').one().total;
      const marks = entries.map(() => '?').join(', ');
      const suppressed = entries.length ? this.sql.exec(
        `SELECT id FROM agents WHERE id IN (${marks})
          AND (listing_state != 'active' OR review_status = 'rejected')`,
        ...entries.map(agent => agent.id)).toArray().map(row => row.id) : [];
      return respond({ status: 'stored', ingested: entries.length, total: count,
        suppressed_ids: suppressed });
    }

    if (request.method === 'POST' && path === '/manage') {
      let data;
      try { data = await request.json(); } catch { return respond({ error: 'Invalid JSON' }, 400); }
      const validActions = new Set(['approve', 'reject', 'reset_review', 'hide', 'deactivate', 'activate']);
      if (!data || !/^[0-9a-f]{20}$/.test(data.id || '') ||
          !validActions.has(data.action) || typeof data.reason !== 'string' ||
          !data.reason.trim() || data.reason.length > 240 || /[\x00-\x1f]/.test(data.reason)) {
        return respond({ error: 'Invalid management request' }, 400);
      }
      const before = this.sql.exec(
        'SELECT listing_state, review_status FROM agents WHERE id=?', data.id).toArray()[0];
      if (!before) return respond({ error: 'Agent unknown' }, 404);
      const listing = ({ hide: 'hidden', deactivate: 'deactivated', activate: 'active' })[data.action]
        || before.listing_state;
      const review = ({ approve: 'approved', reject: 'rejected', reset_review: 'unreviewed' })[data.action]
        || before.review_status;
      const changedAt = new Date().toISOString();
      this.ctx.storage.transactionSync(() => {
        this.sql.exec('UPDATE agents SET listing_state=?, review_status=? WHERE id=?',
          listing, review, data.id);
        this.sql.exec(`INSERT INTO registry_audit
          (agent_id, action, listing_state, review_status, reason, changed_at)
          VALUES (?, ?, ?, ?, ?, ?)`,
          data.id, data.action, listing, review, data.reason.trim(), changedAt);
      });
      return respond({ id: data.id, listing_state: listing, review_status: review, changed_at: changedAt });
    }

    if (request.method === 'GET' && path === '/health/due') {
      const due = this.sql.exec(`SELECT agents.id, json_extract(agents.record, '$.endpoint') AS endpoint
        FROM agents LEFT JOIN agent_health AS health ON health.id=agents.id
        WHERE agents.listing_state='active' AND agents.review_status != 'rejected'
          AND json_extract(agents.record, '$.source') != 'authorized-catalog'
          AND length(json_extract(agents.record, '$.endpoint')) > 0
        ORDER BY CASE WHEN health.endpoint=json_extract(agents.record, '$.endpoint')
          THEN health.checked_at ELSE '' END ASC, agents.id ASC LIMIT 60`).toArray();
      return respond({ entries: due });
    }

    if (request.method === 'POST' && path === '/health') {
      let data;
      try { data = await request.json(); } catch { return respond({ error: 'Invalid JSON' }, 400); }
      const reports = Array.isArray(data?.results) && data.results.length <= 60
        ? data.results.map(normalizeHealthReport) : null;
      if (!reports || reports.some(item => item === null)) {
        return respond({ error: 'Invalid health report' }, 400);
      }
      const timestamp = new Date().toISOString();
      let stored = 0;
      this.ctx.storage.transactionSync(() => {
        for (const item of reports) {
          const current = this.sql.exec(`SELECT record FROM agents WHERE id=?
            AND listing_state='active' AND review_status != 'rejected'`, item.id).toArray()[0];
          if (!current) continue;
          const agent = JSON.parse(current.record);
          if (agent.endpoint !== item.endpoint || agent.source === 'authorized-catalog') continue;
          this.sql.exec(`INSERT INTO agent_health (id, endpoint, status, http_status, checked_at,
              state, redirect, challenge, tls_not_after)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
            endpoint=excluded.endpoint, status=excluded.status,
            http_status=excluded.http_status, checked_at=excluded.checked_at,
            state=excluded.state, redirect=excluded.redirect, challenge=excluded.challenge,
            tls_not_after=excluded.tls_not_after`,
            item.id, item.endpoint, item.status, item.http_status, timestamp,
            item.state, item.redirect ? 1 : 0, item.challenge, item.tls_not_after);
          stored++;
        }
      });
      return respond({ status: 'stored', stored });
    }

    const health = path.match(/^\/health\/([0-9a-f]{20})$/);
    if (request.method === 'GET' && health) {
      const record = this.sql.exec(`SELECT agents.record, agent_health.endpoint, agent_health.status,
        agent_health.http_status, agent_health.checked_at, agent_health.state,
        agent_health.redirect, agent_health.challenge, agent_health.tls_not_after FROM agents
        LEFT JOIN agent_health ON agent_health.id=agents.id
        WHERE agents.id=? AND agents.listing_state='active'
          AND agents.review_status != 'rejected'`, health[1]).toArray()[0];
      if (!record) return respond({ error: 'Agent unknown' }, 404);
      const agent = JSON.parse(record.record);
      if (!record.checked_at || record.endpoint !== agent.endpoint) {
        return respond({ id: health[1], status: 'unchecked', checked_at: null,
          protocol_verified: false });
      }
      const age = Date.now() - Date.parse(record.checked_at);
      if (!Number.isFinite(age) || age < 0 || age > 7 * 86400000) {
        return respond({ id: health[1], status: 'stale', checked_at: record.checked_at,
          protocol_verified: false });
      }
      const observed = healthFacts(record);
      return respond({ id: health[1], status: observed.status,
        http_status: record.http_status, checked_at: record.checked_at,
        protocol_verified: false, state: observed.state, redirect: observed.redirect,
        ...(record.challenge ? { challenge: record.challenge } : {}),
        ...(record.tls_not_after ? { tls_not_after: record.tls_not_after } : {}) });
    }

    const audit = path.match(/^\/audit\/([0-9a-f]{20})$/);
    if (request.method === 'GET' && audit) {
      const agent = this.sql.exec(
        'SELECT listing_state, review_status FROM agents WHERE id=?', audit[1]).toArray()[0];
      if (!agent) return respond({ error: 'Agent unknown' }, 404);
      const history = this.sql.exec(`SELECT action, listing_state, review_status, reason, changed_at
        FROM registry_audit WHERE agent_id=? ORDER BY revision DESC LIMIT 100`, audit[1]).toArray();
      return respond({ id: audit[1], ...agent, history });
    }

    if (request.method === 'GET' && path === '/candidates') {
      const offsetText = url.searchParams.get('offset') || '0';
      if (!/^(0|[1-9]\d*)$/.test(offsetText) || !Number.isSafeInteger(Number(offsetText))) {
        return respond({ error: 'Invalid candidate offset' }, 400);
      }
      const offset = Number(offsetText);
      const rows = this.sql.exec(`SELECT agents.id, agents.record, embedding.vector,
          embedding.input_text, embedding.model FROM agents
        LEFT JOIN agent_embeddings AS embedding ON embedding.id=agents.id
        WHERE agents.listing_state='active' AND agents.review_status != 'rejected'
        ORDER BY agents.updated_at DESC, agents.name_search ASC, agents.id ASC
        LIMIT 101 OFFSET ?`, offset).toArray();
      const page = rows.slice(0, 100);
      const entries = page.map(row => JSON.parse(row.record));
      const cached = page.map((row, i) => {
        if (row.model !== EMBEDDING_MODEL || row.input_text !== descriptionOf(entries[i])) return null;
        try {
          const vector = JSON.parse(row.vector);
          return validEmbeddings([vector], 1) ? vector : null;
        } catch { return null; }
      });
      const missing = page.flatMap((row, i) => cached[i] === null
        ? [{ id: row.id, text: descriptionOf(entries[i]) }] : []);
      const indexed = await this.indexMissingEmbeddings(missing);
      const vectors = cached.map((vector, i) => vector || indexed.get(page[i].id) || null);
      const meta = this.sql.exec(
        "SELECT value FROM registry_meta WHERE key='last_ingested_at'").toArray()[0];
      return respond({ entries, vectors, next_offset: rows.length > 100 ? offset + 100 : null,
        truncated: false, generated_at: meta?.value || null });
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
      const parsed = parseFilters(url.searchParams);
      if (parsed.error) return respond({ error: parsed.error }, 400);
      const extra = parsed.filters;

      const terms = (query.toLowerCase().match(/[\p{L}\p{N}-]+/gu) || []).slice(0, 12);
      const filters = terms.map(() => 'search_text LIKE ?').join(' OR ');
      const visible = "listing_state='active' AND review_status != 'rejected'";
      const where = 'WHERE ' + visible + (filters ? ' AND (' + filters + ')' : '') +
        (extra.skill ? ' AND skills_search LIKE ?' : '');
      const bindings = [...terms.map(term => '%' + term + '%'),
        ...(extra.skill ? ['%' + extra.skill.replace(/[%_\\]/g, ' ') + '%'] : [])];
      const matched = this.sql.exec(`SELECT COUNT(*) AS total FROM agents ${where}`, ...bindings).one().total;
      const total = this.sql.exec(`SELECT COUNT(*) AS total FROM agents WHERE ${visible}`).one().total;
      const relevance = terms.map(() =>
        '(CASE WHEN name_search LIKE ? THEN 8 ELSE 0 END + ' +
        'CASE WHEN skills_search LIKE ? THEN 4 ELSE 0 END + ' +
        'CASE WHEN description_search LIKE ? THEN 1 ELSE 0 END)').join(' + ');
      const sort = relevance ? relevance + ' DESC, name_search ASC' : 'name_search ASC';
      const rankParams = terms.flatMap(term => ['%' + term + '%', '%' + term + '%', '%' + term + '%']);
      const meta = this.sql.exec(
        "SELECT value FROM registry_meta WHERE key='last_ingested_at'").toArray()[0];
      if (extra.computed) {
        // Read-time evidence (level, keyless, freshness) is not a SQL column, so these filters
        // scan the bounded candidate set, then page. Relevance order is kept unless a sort is asked.
        const scanned = this.presentEntries(this.sql.exec(
          `SELECT id, record, review_status, updated_at FROM agents ${where} ORDER BY ${sort} LIMIT ${MAX_FILTER_SCAN}`,
          ...bindings, ...rankParams).toArray());
        const now = new Date();
        const kept = sortEntries(scanned.filter(entry => matchesFilters(entry.item, entry.full, extra, now)),
          extra.sort);
        return respond({
          query, count: kept.length, total, offset, limit,
          generated_at: meta ? meta.value : null,
          results: kept.slice(offset, offset + limit).map(entry => entry.item),
          verified: 'Agent Card metadata only; endpoints are not actively probed',
        });
      }
      const rows = this.sql.exec(
        `SELECT id, record, review_status, updated_at FROM agents ${where} ORDER BY ${sort} LIMIT ? OFFSET ?`,
        ...bindings, ...rankParams, limit, offset).toArray();
      return respond({
        query, count: matched, total, offset, limit,
        generated_at: meta ? meta.value : null,
        results: this.present(rows),
        verified: 'Agent Card metadata only; endpoints are not actively probed',
      });
    }

    if (request.method === 'GET' && path === '/ans') {
      const raw = url.searchParams.get('name');
      const name = normalizeAnsName(raw);
      if (!name) return respond({ error: 'Invalid ANS name' }, 400);
      const limitText = url.searchParams.get('limit') || '20';
      const offsetText = url.searchParams.get('offset') || '0';
      if (!/^\d{1,2}$/.test(limitText) || !/^\d{1,6}$/.test(offsetText)) {
        return respond({ error: 'Invalid ANS pagination' }, 400);
      }
      const limit = Number(limitText), offset = Number(offsetText);
      if (limit < 1 || limit > 20 || offset > 100000) {
        return respond({ error: 'Invalid ANS pagination' }, 400);
      }
      const visible = "ans_name=? AND listing_state='active' AND review_status != 'rejected'";
      const count = this.sql.exec(
        `SELECT COUNT(*) AS total FROM agents WHERE ${visible}`, name).one().total;
      if (!count) return respond({ error: 'ANS name not found in registry' }, 404);
      const rows = this.sql.exec(`SELECT id, record, review_status, updated_at FROM agents
        WHERE ${visible} ORDER BY name_search ASC, id ASC LIMIT ? OFFSET ?`,
        name, limit, offset).toArray();
      return respond({ name: raw, count, limit, offset,
        truncated: offset + rows.length < count,
        ambiguous: count > 1, identity_verified: false,
        results: this.present(rows) });
    }
    const idMatch = path.match(/^\/agent\/([0-9a-f]{20})$/);
    if (request.method === 'GET' && idMatch) {
      const record = this.sql.exec(`SELECT id, record, review_status, updated_at FROM agents
        WHERE id=? AND listing_state='active' AND review_status != 'rejected'`, idMatch[1]).toArray()[0];
      return record
        ? respond(this.present([record])[0])
        : respond({ error: 'Agent unknown' }, 404);
    }
    if (request.method === 'GET' && (path === '/providers' || path === '/status')) {
      const entries = this.presentEntries(this.sql.exec(`SELECT id, record, review_status, updated_at
        FROM agents WHERE listing_state='active' AND review_status != 'rejected'
        ORDER BY name_search ASC, id ASC LIMIT ${MAX_FILTER_SCAN}`).toArray());
      const meta = Object.fromEntries(this.sql.exec('SELECT key, value FROM registry_meta').toArray()
        .map(row => [row.key, row.value]));
      return path === '/providers'
        ? respondProviders(entries, url.searchParams, meta)
        : respondStatus(entries, meta, this.sql.exec('SELECT MAX(checked_at) AS last FROM agent_health')
          .toArray()[0]?.last || null);
    }
    const verification = path.match(/^\/verification\/([0-9a-f]{20})$/);
    if (request.method === 'GET' && verification) {
      const row = this.sql.exec(`SELECT id, record, review_status, updated_at FROM agents
        WHERE id=? AND listing_state='active' AND review_status != 'rejected'`,
        verification[1]).toArray()[0];
      return row ? respond(this.verificationFor([row], new Date())[0].full)
        : respond({ error: 'Agent unknown' }, 404);
    }
    return respond({ error: 'Not found' }, 404);
  }
}

const TLS_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

/**
 * Validate one scheduled health report. The honest state is recomputed from the HTTP code,
 * so an older crawler that calls a 404 "responding" can never store that claim.
 * Returns null for a malformed report.
 */
function normalizeHealthReport(item) {
  if (!item || typeof item !== 'object' || !/^[0-9a-f]{20}$/.test(item.id || '') ||
      typeof item.endpoint !== 'string' || !item.endpoint.startsWith('https://') ||
      item.endpoint.length > 1000 || !HEALTH_STATUSES.has(item.status)) return null;
  const code = item.http_status;
  if (!(code === null || (Number.isInteger(code) && code >= 100 && code <= 599))) return null;
  const noResponse = item.status === 'unreachable' || item.status === 'skipped';
  if (noResponse !== (code === null)) return null;
  let state;
  if (noResponse) {
    state = item.state ?? item.status;
    if (HEALTH_STATE_STATUS[state] !== item.status) return null;
  } else {
    const redirect = item.redirect === true;
    state = stateFromHttp(code, { redirectTargetValid: redirect && item.state === 'reachable' });
    if (item.state !== undefined && (item.state !== state ||
        HEALTH_STATE_STATUS[item.state] !== item.status)) return null;
    if (item.state === undefined && item.status === 'responding' && code >= 500) return null;
  }
  if (item.challenge !== undefined && item.challenge !== null && !CHALLENGES.has(item.challenge)) return null;
  if (item.tls_not_after !== undefined && item.tls_not_after !== null &&
      !(typeof item.tls_not_after === 'string' && TLS_TIME.test(item.tls_not_after))) return null;
  return {
    id: item.id, endpoint: item.endpoint, http_status: code, state,
    status: HEALTH_STATE_STATUS[state], redirect: item.redirect === true && code !== null && code < 400,
    challenge: item.challenge ?? null, tls_not_after: item.tls_not_after ?? null,
  };
}

/** Stored health row to honest facts; rows written before the taxonomy are reclassified. */
function healthFacts(row) {
  const state = row.state || (row.http_status === null
    ? (row.status === 'skipped' ? 'skipped' : 'unreachable') : stateFromHttp(row.http_status));
  return { state, status: HEALTH_STATE_STATUS[state] || row.status, redirect: row.redirect === 1 };
}

const LEVEL_KEYS = ['0', '1', '2', '3'];
const histogram = () => Object.fromEntries(LEVEL_KEYS.map(key => [key, 0]));

/** Group visible agents by what the publishers declared. Names are publisher text. */
function respondProviders(entries, params, meta) {
  const kind = params.get('kind') || 'declared_provider';
  const minAgents = Number(params.get('min_agents') || '1');
  const limit = Number(params.get('limit') || '50');
  const offset = Number(params.get('offset') || '0');
  if (!['declared_provider', 'hosting', 'model_provider'].includes(kind) ||
      !Number.isInteger(minAgents) || minAgents < 1 || minAgents > 1000 ||
      !Number.isInteger(limit) || limit < 1 || limit > 50 ||
      !Number.isInteger(offset) || offset < 0 || offset > 200) {
    return respond({ error: 'Invalid providers request' }, 400);
  }
  const groups = new Map();
  for (const { item } of entries) {
    let name = null, urlHost = null, basis = 'card.provider', confidence = 'declared';
    if (kind === 'declared_provider') {
      name = item.backing?.declared_provider?.organization || item.provider?.organization || null;
      try { urlHost = item.provider?.url ? new URL(item.provider.url).hostname : null; } catch { urlHost = null; }
    } else if (kind === 'hosting') {
      const hosting = item.backing?.hosting;
      name = hosting && hosting.platform !== 'unknown' ? hosting.platform : null;
      basis = 'endpoint_host_suffix'; confidence = 'inferred_from_host';
    } else {
      name = item.backing?.model_provider?.name || null;
      basis = 'card_extension';
    }
    if (!name) continue;
    const key = name.toLowerCase();
    const group = groups.get(key) || { name, url_host: urlHost, agents: 0, keyless_n: 0,
      level_histogram: histogram(), basis, confidence };
    group.agents++;
    if (item.access?.keyless === 'declared') group.keyless_n++;
    group.level_histogram[String(item.verification_detail.level)]++;
    groups.set(key, group);
  }
  const all = [...groups.values()].filter(group => group.agents >= minAgents)
    .sort((a, b) => b.agents - a.agents || a.name.localeCompare(b.name));
  return respond({ kind, total: all.length, offset, limit,
    registry_generated_at: meta.last_ingested_at || null,
    sources: all.slice(offset, offset + limit) });
}

function respondStatus(entries, meta, lastProbe) {
  const levels = histogram();
  let withEndpoint = 0, probed = 0, withRaw = 0, withProvider = 0;
  for (const { item, full } of entries) {
    levels[String(item.verification_detail.level)]++;
    if (item.raw_card) withRaw++;
    if (item.provider || item.backing?.declared_provider) withProvider++;
    if (item.endpoint) {
      withEndpoint++;
      if (full.checks.endpoint.state !== 'unchecked' && full.checks.endpoint.state !== 'expired') probed++;
    }
  }
  return respond({
    visible_agents: entries.length, with_raw_card: withRaw, with_provider: withProvider,
    level_histogram: levels, last_ingest_at: meta.last_ingested_at || null,
    last_probe_at: lastProbe, probe_coverage_7d: withEndpoint ? probed / withEndpoint : 0,
    schema_revision: '1.1',
  });
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
    if (!env.REGISTRY) return directoryReply({ error: 'Registry not configured' }, 503);
    try {
      const entries = [];
      const candidateVectors = [];
      let offset = 0;
      let generatedAt = null;
      const registry = registryStub(env);
      while (true) {
        const candidates = await registry.fetch(
          new Request('https://registry.internal/candidates?offset=' + offset));
        if (!candidates.ok) throw new Error('Registry unavailable');
        const data = await candidates.json();
        if (!Array.isArray(data.entries)) throw new Error('Invalid registry candidates');
        if (data.vectors !== undefined &&
            (!Array.isArray(data.vectors) || data.vectors.length !== data.entries.length)) {
          throw new Error('Invalid registry embedding page');
        }
        generatedAt = data.generated_at ?? generatedAt;
        entries.push(...data.entries.map(agent => ({
          ...agent, kind: 'a2a-agent', installable: false,
        })));
        candidateVectors.push(...(data.vectors || data.entries.map(() => null)));
        if (data.next_offset == null) break;
        if (!Number.isSafeInteger(data.next_offset) ||
            data.next_offset <= offset || data.entries.length !== 100) {
          throw new Error('Invalid registry pagination');
        }
        offset = data.next_offset;
      }
      const ranked = await rankAgentCandidates(query, env, entries, generatedAt, candidateVectors);
      return directoryReply({ ...ranked, candidate_scope: 'persistent-registry',
        candidate_count: entries.length, truncated: false });
    } catch { return directoryReply({ error: 'Indexed directory temporarily unavailable' }, 502); }
  }
  // Stateless, read-only MCP endpoint. Reads the registry only; disabled unless MCP_ENABLED="true".
  if (url.pathname === '/mcp') {
    return handleMcp(request, env, path => registryStub(env).fetch(new Request('https://registry.internal' + path)));
  }
  // Read-only registry endpoints are public APIs, including to other web apps.
  const isPublic = url.pathname === '/api/v1/agents' ||
    /^\/api\/v1\/agents\/[0-9a-f]{20}\/health$/.test(url.pathname) ||
    url.pathname.startsWith('/api/v1/agents/') ||
    url.pathname === '/api/v1/ans/resolve';
  const publicReply = (body, status) => {
    const result = respond(body, status);
    result.headers.set('Access-Control-Allow-Origin', '*');
    return result;
  };
  if (request.method === 'OPTIONS') {
    if (isPublic) {
      const result = publicReply({}, 204);
      result.headers.set('Access-Control-Allow-Methods', 'GET, OPTIONS');
      return result;
    }
    return origin === allowedOrigin ? reply({}, 204) : reply({ error: 'Origin not allowed' }, 403);
  }
  // Legacy POST dispatch is deliberately retired; all interactive searches are registry reads.
  if (request.method === 'POST' && url.pathname === '/api/v1/search') {
    return reply({ error: 'Per-query crawling retired. Use GET /api/v1/agents?query=...' }, 410);
  }
  if (request.method === 'GET' && isPublic) {
    if (!env.REGISTRY) return publicReply({ error: 'Registry not configured' }, 503);
    const path = url.pathname === '/api/v1/agents'
      ? '/search' + url.search
      : /^\/api\/v1\/agents\/[0-9a-f]{20}\/health$/.test(url.pathname)
        ? '/health/' + url.pathname.split('/')[4]
      : /^\/api\/v1\/agents\/[0-9a-f]{20}\/verification$/.test(url.pathname)
        ? '/verification/' + url.pathname.split('/')[4]
      : url.pathname === '/api/v1/ans/resolve'
        ? '/ans' + url.search
        : '/agent/' + url.pathname.slice('/api/v1/agents/'.length);
    const result = await registryStub(env).fetch(new Request('https://registry.internal' + path));
    return publicReply(await result.json(), result.status);
  }
  if ((request.method === 'GET' && url.pathname === '/internal/registry/health/due') ||
      (request.method === 'POST' && url.pathname === '/internal/registry/health')) {
    if (!env.RESULT_CALLBACK_SECRET ||
        request.headers.get('Authorization') !== `Bearer ${env.RESULT_CALLBACK_SECRET}`) {
      return reply({ error: 'Unauthorized' }, 401);
    }
    if (!env.REGISTRY) return reply({ error: 'Registry not configured' }, 503);
    let body;
    if (request.method === 'POST') {
      body = await request.text();
      if (body.length > 20000) return reply({ error: 'Health report too large' }, 413);
    }
    const result = await registryStub(env).fetch(new Request(
      'https://registry.internal' + (request.method === 'GET' ? '/health/due' : '/health'), {
        method: request.method, ...(body === undefined ? {} : { body }),
      }));
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
  // Curation is deliberately separate from the crawler's ingestion credential.
  if ((request.method === 'POST' && url.pathname === '/internal/registry/manage') ||
      (request.method === 'GET' && /^\/internal\/registry\/audit\/[0-9a-f]{20}$/.test(url.pathname))) {
    if (!env.REGISTRY_ADMIN_SECRET ||
        request.headers.get('Authorization') !== `Bearer ${env.REGISTRY_ADMIN_SECRET}`) {
      return reply({ error: 'Unauthorized' }, 401);
    }
    if (!env.REGISTRY) return reply({ error: 'Registry not configured' }, 503);
    let path = '/audit/' + url.pathname.slice('/internal/registry/audit/'.length);
    let body;
    if (request.method === 'POST') {
      path = '/manage';
      body = await request.text();
      if (body.length > 4096) return reply({ error: 'Management request too large' }, 413);
    }
    const managed = await registryStub(env).fetch(new Request('https://registry.internal' + path, {
      method: request.method, ...(body === undefined ? {} : { body }),
    }));
    return reply(await managed.json(), managed.status);
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
