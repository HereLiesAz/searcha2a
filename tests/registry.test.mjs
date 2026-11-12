import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { AgentRegistry, handle } from '../worker/index.mjs';

function registryEnvironment() {
  // Use a real SQLite engine; expected results below are authored independently.
  const database = new DatabaseSync(':memory:');
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
