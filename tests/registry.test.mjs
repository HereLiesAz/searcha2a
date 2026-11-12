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

test('ANS names with opaque publisher-defined versions resolve only curated claims', async () => {
  const state = registryEnvironment();
  try {
    const named = { ...weather, ans_name: 'ans://release-2026.11.weather.example.com',
      ans_host: 'weather.example.com' };
    assert.equal((await ingest(state.env, [named])).status, 200);
    const url = '/api/v1/ans/resolve?name=' + encodeURIComponent(named.ans_name);
    let response = await handle(request(url), state.env);
    assert.equal(response.status, 200);
    let body = await response.json();
    assert.equal(body.count, 1);
    assert.equal(body.identity_verified, false);
    assert.equal(body.results[0].ans.version, 'release-2026.11');
    assert.equal(body.results[0].ans.verification, 'unverified');
    assert.equal((await handle(request('/api/v1/ans/resolve?name=bogus'), state.env)).status, 400);
    assert.equal((await manage(state.env, weather.id, 'hide', 'Claim withdrawn')).status, 200);
    assert.equal((await handle(request(url), state.env)).status, 404);
    assert.equal((await ingest(state.env, [{ ...named, ans_name: 'ans://new.weather.example.com',
      ans_host: 'attacker.example.com' }])).status, 200);
    assert.equal((await handle(request(url), state.env)).status, 404);
  } finally { state.close(); }
});

test('invalid ANS claim cannot pollute indexed agent record', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [{ ...weather, ans_name: 'ans://1.0.0.other.example',
      ans_host: 'weather.example.com', ans: { verification: 'verified' } }]);
    const result = (await (await lookup(state.env, 'weather')).json()).results[0];
    assert.equal(Object.hasOwn(result, 'ans'), false);
    assert.equal((await handle(request('/api/v1/ans/resolve?name=ans%3A%2F%2F1.0.0.other.example'),
      state.env)).status, 404);
  } finally { state.close(); }
});

test('unified ANS capability directory ranks durable curated records without fetching a feed', async () => {
  const state = registryEnvironment();
  const savedFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('Semantic discovery must not fetch the public snapshot'); };
  try {
    const named = { ...weather, ans_name: 'ans://spring.2026.weather.example.com',
      ans_host: 'weather.example.com' };
    await ingest(state.env, [named, calendar]);
    state.env.SEARCH_RATE_LIMITER = { limit: async () => ({ success: true }) };
    let response = await handle(request('/api/v1/directory?q=forecast'), state.env);
    assert.equal(response.status, 200);
    let result = await response.json();
    assert.equal(result.candidate_scope, 'persistent-registry');
    assert.equal(result.truncated, false);
    assert.equal(result.match_mode, 'keyword-fallback');
    assert.deepEqual(result.results.map(agent => agent.name), ['Weather Expert']);
    assert.equal(result.results[0].ans.version, 'spring.2026');
    assert.equal(result.results[0].installable, false);
    await manage(state.env, weather.id, 'deactivate', 'No longer offered');
    response = await handle(request('/api/v1/directory?q=forecast'), state.env);
    result = await response.json();
    assert.deepEqual(result.results, []);
  } finally {
    globalThis.fetch = savedFetch;
    state.close();
  }
});

test('ANS resolution exposes duplicate publisher claims without selecting an identity', async () => {
  const state = registryEnvironment();
  try {
    const name = 'ans://2026-Q4.agents.example.com';
    await ingest(state.env, [
      { ...weather, ans_name: name, ans_host: 'agents.example.com' },
      { ...calendar, ans_name: name, ans_host: 'agents.example.com' },
    ]);
    const response = await handle(request('/api/v1/ans/resolve?name=' +
      encodeURIComponent(name)), state.env);
    assert.equal(response.status, 200);
    const data = await response.json();
    assert.equal(data.count, 2);
    assert.equal(data.ambiguous, true);
    assert.equal(data.truncated, false);
    assert.equal(data.identity_verified, false);
    assert.deepEqual(data.results.map(item => item.name), ['Calendar Helper', 'Weather Expert']);
  } finally { state.close(); }
});

test('semantic directory paginates every persisted card and honors moderation beyond 150', async () => {
  const state = registryEnvironment();
  try {
    const records = Array.from({ length: 205 }, (_, i) => card(
      i.toString(16).padStart(20, '0'),
      'Agent ' + i.toString().padStart(3, '0'),
      i === 204 ? 'starlingcapability' : 'Routine',
    ));
    for (let i = 0; i < records.length; i += 70) {
      assert.equal((await ingest(state.env, records.slice(i, i + 70))).status, 200);
    }
    assert.equal((await (await lookup(state.env)).json()).total, 205);
    let response = await handle(request('/api/v1/directory?q=starlingcapability'), state.env);
    assert.equal(response.status, 200);
    let data = await response.json();
    assert.equal(data.truncated, false);
    assert.deepEqual(data.results.map(agent => agent.id), [records[204].id]);
    assert.equal((await manage(state.env, records[204].id, 'reject', 'Unsafe listing')).status, 200);
    response = await handle(request('/api/v1/directory?q=starlingcapability'), state.env);
    data = await response.json();
    assert.equal(data.results.length, 0);
    await ingest(state.env, [records[204]]);
    assert.equal((await (await handle(request('/api/v1/directory?q=starlingcapability'),
      state.env)).json()).results.length, 0);
    assert.equal((await manage(state.env, records[203].id, 'deactivate', 'Unavailable')).status, 200);
    const candidates = await state.env.REGISTRY.get().fetch(request('/candidates?offset=100'));
    assert.equal(candidates.status, 200);
    const page = await candidates.json();
    assert.equal(page.entries.length, 100);
    assert.equal(page.next_offset, 200);
  } finally { state.close(); }
});

test('partial A2A cards are indexed with empty skills without accepting generic JSON', async () => {
  const state = registryEnvironment();
  try {
    const partial = {
      ...weather,
      id: 'dddddddddddddddddddd',
      name: 'No Skills Yet',
      skills: [],
      protocols: ['JSONRPC'],
      schema_status: 'partial_agent_card',
      raw_card: {
        name: 'No Skills Yet',
        supportedInterfaces: [{ url: 'https://agents.example/a2a',
                                protocolBinding: 'JSONRPC', protocolVersion: '1.0' }],
      },
    };
    assert.equal((await ingest(state.env, [partial])).status, 200);
    const body = await (await lookup(state.env, 'no skills')).json();
    assert.equal(body.count, 1);
    assert.deepEqual(body.results[0].skills, []);
    assert.equal(body.results[0].schema_status, 'partial_agent_card');
    assert.equal(body.results[0].raw_card.name, 'No Skills Yet');
    assert.equal((await ingest(state.env, [{ ...partial, id: 'eeeeeeeeeeeeeeeeeeee',
      schema_status: 'unknown' }])).status, 400);
    assert.equal((await ingest(state.env, [{ ...partial, id: 'ffffffffffffffffffff',
      endpoint: '' }])).status, 400);
    assert.equal((await (await lookup(state.env)).json()).total, 1);
  } finally { state.close(); }
});

test('ANS collision pagination exposes every claimant in deterministic order', async () => {
  const state = registryEnvironment();
  try {
    const name = 'ans://nightly.agent.example.org';
    const claimants = Array.from({ length: 23 }, (_, i) => ({
      ...card(i.toString(16).padStart(20, '0'),
        'Claim ' + i.toString().padStart(2, '0'), 'Forecast'),
      ans_name: name, ans_host: 'agent.example.org',
    }));
    assert.equal((await ingest(state.env, claimants)).status, 200);
    const url = '/api/v1/ans/resolve?name=' + encodeURIComponent(name);
    const first = await (await handle(request(url), state.env)).json();
    assert.equal(first.count, 23);
    assert.equal(first.results.length, 20);
    assert.equal(first.offset, 0);
    assert.equal(first.limit, 20);
    assert.equal(first.truncated, true);
    assert.equal(first.identity_verified, false);
    assert.equal(first.ambiguous, true);
    const last = await (await handle(request(url + '&offset=20&limit=3'), state.env)).json();
    assert.equal(last.count, 23);
    assert.equal(last.results.length, 3);
    assert.equal(last.truncated, false);
    assert.deepEqual(last.results.map(a => a.name), ['Claim 20', 'Claim 21', 'Claim 22']);
    assert.equal((await handle(request(url + '&limit=21'), state.env)).status, 400);
    assert.equal((await handle(request(url + '&offset=-1'), state.env)).status, 400);
    assert.equal((await handle(request(url + '&offset=100001'), state.env)).status, 400);
  } finally { state.close(); }
});
