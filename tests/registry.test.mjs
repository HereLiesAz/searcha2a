import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { AgentRegistry, handle } from '../worker/index.mjs';

function registryEnvironment({ legacySchema = false } = {}) {
  // Use a real SQLite engine; expected results below are authored independently.
  const database = new DatabaseSync(':memory:');
  if (legacySchema) {
    database.exec(`CREATE TABLE agents (
      id TEXT PRIMARY KEY, name_search TEXT NOT NULL, skills_search TEXT NOT NULL,
      description_search TEXT NOT NULL, search_text TEXT NOT NULL,
      record TEXT NOT NULL, updated_at TEXT NOT NULL
    )`);
  }
  const storage = {
    sql: {
      exec(query, ...args) {
        const stmt = database.prepare(query);
        if (/^\s*SELECT\b/i.test(query)) {
          const rows = stmt.all(...args);
          return {
            toArray: () => rows,
            one: () => {
              if (rows.length !== 1) throw new Error('SQL cursor expected exactly one row');
              return rows[0];
            },
          };
        }
        stmt.run(...args);
        return { toArray: () => [], one: () => { throw new Error('No returned row'); } };
      },
    },
    transactionSync(fn) {
      database.exec('BEGIN');
      try {
        const value = fn();
        database.exec('COMMIT');
        return value;
      } catch (error) {
        database.exec('ROLLBACK');
        throw error;
      }
    },
  };
  const object = new AgentRegistry({ storage });
  return {
    env: {
      ALLOWED_ORIGIN: 'https://searcha2a.azphalt.store',
      RESULT_CALLBACK_SECRET: 'pipeline-secret',
      REGISTRY_ADMIN_SECRET: 'admin-only-secret',
      REGISTRY: { idFromName: name => name, get: () => object },
    },
    close: () => database.close(),
  };
}

const card = (id, name, skill, description = 'Independent A2A agent') => ({
  id, name, description, endpoint: 'https://agents.example/a2a',
  source_url: `https://agents.example/cards/${id}.json`,
  skills: [{ name: skill, description: skill + ' service', tags: [skill.toLowerCase()] }],
  source: 'registry', verification: 'card_read_only', protocols: [],
  capabilities: {}, security_schemes: [], version: '1',
});
const weather = card('aaaaaaaaaaaaaaaaaaaa', 'Weather Expert', 'Forecast', 'Rain and clouds');
const calendar = card('bbbbbbbbbbbbbbbbbbbb', 'Calendar Helper', 'Scheduling', 'Meetings and events');
const request = (url, options = {}) => new Request('https://api.example' + url, {
  ...options, headers: { Origin: 'https://searcha2a.azphalt.store', ...options.headers },
});
const crawl = records => ({ query: '', results: records, generated_at: '2026-11-12T12:00:00Z' });
const ingest = (env, records, authorization = 'Bearer pipeline-secret') =>
  handle(request('/internal/registry/ingest', {
    method: 'POST', headers: { Authorization: authorization },
    body: JSON.stringify(crawl(records)),
  }), env);
const lookup = (env, query = '') =>
  handle(request('/api/v1/agents?limit=70&query=' + encodeURIComponent(query)), env);

test('new registry responds instantly with zero records before first crawl', async () => {
  const state = registryEnvironment();
  try {
    const response = await lookup(state.env, 'weather');
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.deepEqual(body.results, []);
    assert.equal(body.count, 0);
    assert.equal(body.total, 0);
    assert.equal(body.generated_at, null);
  } finally { state.close(); }
});

test('authenticated crawls build and update a persistent searchable registry', async () => {
  const state = registryEnvironment();
  try {
    assert.equal((await ingest(state.env, [calendar, weather])).status, 200);
    let response = await lookup(state.env, 'weather');
    let body = await response.json();
    assert.equal(body.count, 1);
    assert.equal(body.total, 2);
    assert.equal(body.results[0].name, 'Weather Expert');
    assert.equal(body.results[0].id, 'aaaaaaaaaaaaaaaaaaaa');
    assert.equal((await lookup(state.env, 'forecast')).status, 200);

    const updated = { ...weather, description: 'Hourly temperature forecast' };
    assert.equal((await ingest(state.env, [updated])).status, 200);
    body = await (await lookup(state.env)).json();
    assert.equal(body.total, 2); // An unseen agent is not deleted by a later crawl.
    assert.deepEqual(body.results.map(x => x.name), ['Calendar Helper', 'Weather Expert']);
    assert.equal((await (await lookup(state.env, 'hourly')).json()).results[0].description,
      'Hourly temperature forecast');
    const detail = await handle(request('/api/v1/agents/bbbbbbbbbbbbbbbbbbbb'), state.env);
    assert.equal((await detail.json()).name, 'Calendar Helper');
    assert.equal((await handle(request('/api/v1/agents/cccccccccccccccccccc'), state.env)).status, 404);
  } finally { state.close(); }
});

test('batch secret and validation forbid unsolicited or partially invalid registrations', async () => {
  const state = registryEnvironment();
  try {
    assert.equal((await ingest(state.env, [weather], 'Bearer wrong')).status, 401);
    const invalid = await ingest(state.env, [weather, { ...calendar, verification: 'untrusted' }]);
    assert.equal(invalid.status, 400);
    assert.equal((await (await lookup(state.env)).json()).total, 0);
    assert.equal((await ingest(state.env, [weather])).status, 200);
    assert.equal((await ingest(state.env, [])).status, 200);
    assert.equal((await (await lookup(state.env)).json()).total, 1);
  } finally { state.close(); }
});

test('persistent registry GET supports cross-origin clients without credentials', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [weather, calendar]);
    const response = await handle(new Request('https://api.example/api/v1/agents?query=weather', {
      headers: { Origin: 'https://other-client.example' },
    }), state.env);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('Access-Control-Allow-Origin'), '*');
    const body = await response.json();
    assert.equal(body.count, 1);
    assert.equal(body.results[0].name, 'Weather Expert');
    const preflight = await handle(new Request('https://api.example/api/v1/agents', {
      method: 'OPTIONS', headers: { Origin: 'https://other-client.example' },
    }), state.env);
    assert.equal(preflight.status, 204);
    assert.equal(preflight.headers.get('Access-Control-Allow-Origin'), '*');
  } finally { state.close(); }
});

test('search pagination and query limits are bounded without GitHub requests', async () => {
  const state = registryEnvironment();
  const savedFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('Registry query must never fetch remote services'); };
  try {
    await ingest(state.env, [calendar, weather]);
    const page = await handle(request('/api/v1/agents?limit=1&offset=1'), state.env);
    const body = await page.json();
    assert.equal(body.count, 2);
    assert.equal(body.results.length, 1);
    assert.equal(body.results[0].name, 'Weather Expert');
    assert.equal((await handle(request('/api/v1/agents?limit=101'), state.env)).status, 400);
    assert.equal((await handle(request('/api/v1/agents?query=' + 'a'.repeat(121)), state.env)).status, 400);
    assert.equal((await (await lookup(state.env, 'unmatched')).json()).count, 0);
  } finally {
    globalThis.fetch = savedFetch;
    state.close();
  }
});

const manage = (env, id, action, reason = 'Curator decision', authorization = 'Bearer admin-only-secret') =>
  handle(request('/internal/registry/manage', {
    method: 'POST',
    headers: { Authorization: authorization },
    body: JSON.stringify({ id, action, reason }),
  }), env);
const audit = (env, id, authorization = 'Bearer admin-only-secret') =>
  handle(request('/internal/registry/audit/' + id, {
    headers: { Authorization: authorization },
  }), env);

test('existing v2 tables migrate without deleting previously indexed agents', async () => {
  const state = registryEnvironment({ legacySchema: true });
  try {
    assert.equal((await ingest(state.env, [weather])).status, 200);
    const body = await (await lookup(state.env, 'forecast')).json();
    assert.equal(body.count, 1);
    assert.equal(body.results[0].curation_status, 'unreviewed');
  } finally { state.close(); }
});

test('crawler secret cannot approve, hide, or read private audit history', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [weather]);
    const id = weather.id;
    assert.equal((await manage(state.env, id, 'approve', 'No', 'Bearer pipeline-secret')).status, 401);
    assert.equal((await audit(state.env, id, 'Bearer pipeline-secret')).status, 401);
    assert.equal((await audit({ ...state.env, REGISTRY_ADMIN_SECRET: undefined }, id)).status, 401);
    const published = await (await lookup(state.env, 'weather')).json();
    assert.equal(published.results[0].curation_status, 'unreviewed');
  } finally { state.close(); }
});

test('review decisions and listing deactivation survive later scheduled crawls', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [weather, calendar]);
    assert.equal((await manage(state.env, weather.id, 'approve', 'Reviewed Agent Card')).status, 200);
    let visible = await (await lookup(state.env, 'weather')).json();
    assert.equal(visible.results[0].curation_status, 'approved');
    // Approval is metadata review. It never upgrades the card verification claim.
    assert.equal(visible.results[0].verification, 'card_read_only');

    assert.equal((await manage(state.env, weather.id, 'deactivate', 'Publisher asked to unlist')).status, 200);
    visible = await (await lookup(state.env, 'weather')).json();
    assert.equal(visible.count, 0);
    assert.equal(visible.total, 1);
    assert.equal((await handle(request('/api/v1/agents/' + weather.id), state.env)).status, 404);

    // A newly fetched card cannot resurrect a curator-deactivated listing.
    await ingest(state.env, [{ ...weather, description: 'Newly recrawled description' }]);
    visible = await (await lookup(state.env, 'weather')).json();
    assert.equal(visible.count, 0);
    assert.equal((await manage(state.env, weather.id, 'activate', 'Publisher returned')).status, 200);
    visible = await (await lookup(state.env, 'weather')).json();
    assert.equal(visible.count, 1);
    assert.equal(visible.results[0].description, 'Newly recrawled description');
    assert.equal(visible.results[0].curation_status, 'approved');

    const changes = await (await audit(state.env, weather.id)).json();
    assert.deepEqual(changes.history.map(entry => entry.action),
      ['activate', 'deactivate', 'approve']);
    assert.equal(changes.history[1].reason, 'Publisher asked to unlist');
  } finally { state.close(); }
});

test('rejected cards remain invisible even if a curator activates their listing', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [weather]);
    assert.equal((await manage(state.env, weather.id, 'reject', 'Suspected injection')).status, 200);
    assert.equal((await (await lookup(state.env)).json()).count, 0);
    await ingest(state.env, [weather]);
    assert.equal((await manage(state.env, weather.id, 'activate', 'Restored availability')).status, 200);
    assert.equal((await (await lookup(state.env)).json()).total, 0);
    assert.equal((await manage(state.env, weather.id, 'reset_review', 'Review reset')).status, 200);
    const listed = await (await lookup(state.env)).json();
    assert.equal(listed.total, 1);
    assert.equal(listed.results[0].curation_status, 'unreviewed');
  } finally { state.close(); }
});

test('invalid moderation requests cannot modify records or audit history', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [weather]);
    assert.equal((await manage(state.env, weather.id, 'delete', 'Unrecognized')).status, 400);
    assert.equal((await manage(state.env, weather.id, 'approve', '')).status, 400);
    assert.equal((await manage(state.env, 'ffffffffffffffffffff', 'hide')).status, 404);
    const result = await (await audit(state.env, weather.id)).json();
    assert.equal(result.review_status, 'unreviewed');
    assert.deepEqual(result.history, []);
    assert.equal((await (await lookup(state.env)).json()).total, 1);
  } finally { state.close(); }
});
