import test from 'node:test';
import assert from 'node:assert/strict';
import { handle, SearchResult } from '../worker/index.mjs';

function mockEnvironment() {
  const mailboxes = new Map();
  const alarms = new Map();
  return {
    ALLOWED_ORIGIN: 'https://searcha2a.azphalt.store',
    RESULTS: {
      idFromName: id => id,
      get(id) {
        if (!mailboxes.has(id)) {
          const values = new Map();
          mailboxes.set(id, new SearchResult({ storage: {
            kv: {
              get: key => values.get(key),
              put: (key, value) => values.set(key, structuredClone(value)),
              delete: key => values.delete(key),
            },
            setAlarm: async when => alarms.set(id, when),
            deleteAll: async () => { values.clear(); alarms.delete(id); },
          } }));
        }
        return mailboxes.get(id);
      },
    },
    alarms,
    RESULT_CALLBACK_SECRET: 'secret',
  };
}

const req = (url, options = {}) => new Request('https://searcha2a-api.example.com' + url,
  { ...options, headers: { Origin: 'https://searcha2a.azphalt.store', ...(options.headers || {}) } });

test('retired per-query endpoint cannot dispatch GitHub Actions', async () => {
  const env = mockEnvironment();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('Search must not call GitHub'); };
  try {
    const response = await handle(req('/api/v1/search', {
      method: 'POST', body: JSON.stringify({ query: 'weather', turnstile: 'old' }),
    }), env);
    assert.equal(response.status, 410);
    assert.match((await response.json()).error, /retired/);
  } finally { globalThis.fetch = originalFetch; }
});

test('callback requires secret and matching pending request', async () => {
  const env = mockEnvironment();
  const id = 'aab1cc22-1111-4111-8111-123456789abc';
  await env.RESULTS.get(env.RESULTS.idFromName(id)).fetch(new Request('https://results.internal/create', {
    method: 'PUT', body: JSON.stringify({ id, query: 'weather' }),
  }));
  let response = await handle(req('/internal/results', { method: 'POST',
    body: JSON.stringify({ request_id: id, query: 'weather', results: [{ name: 'Weather' }] }) }), env);
  assert.equal(response.status, 401);
  response = await handle(req('/internal/results', { method: 'POST',
    headers: { Authorization: 'Bearer secret' },
    body: JSON.stringify({ request_id: id, query: 'climate', results: [] }) }), env);
  assert.equal(response.status, 409);
  response = await handle(req('/internal/results', { method: 'POST',
    headers: { Authorization: 'Bearer secret' },
    body: JSON.stringify({ request_id: id, query: 'weather', results: [{ name: 'Weather' }] }) }), env);
  assert.equal(response.status, 200);
  const result = await handle(req(`/api/v1/results/${id}`), env);
  assert.equal((await result.json()).results[0].name, 'Weather');
});

test('unknown result id is a 404', async () => {
  const response = await handle(req('/api/v1/results/aab1cc22-1111-4111-8111-123456789abc'), mockEnvironment());
  assert.equal(response.status, 404);
});

test('OPTIONS preflight returns empty 204 and CORS headers', async () => {
  const response = await handle(req('/api/v1/search', { method: 'OPTIONS' }), mockEnvironment());
  assert.equal(response.status, 204);
  assert.equal(await response.text(), '');
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), 'https://searcha2a.azphalt.store');
});

test('a completed result cannot be overwritten by a duplicate callback', async () => {
  const env = mockEnvironment();
  const id = 'aab1cc22-1111-4111-8111-123456789abc';
  await env.RESULTS.get(id).fetch(new Request('https://results.internal/create', {
    method: 'PUT', body: JSON.stringify({ id, query: 'weather' }),
  }));
  const callback = results => handle(req('/internal/results', {
    method: 'POST', headers: { Authorization: 'Bearer secret' },
    body: JSON.stringify({ request_id: id, query: 'weather', results }),
  }), env);
  assert.equal((await callback([{ name: 'Original' }])).status, 200);
  assert.equal((await callback([{ name: 'Overwrite' }])).status, 409);
  const result = await handle(req(`/api/v1/results/${id}`), env);
  assert.equal((await result.json()).results[0].name, 'Original');
});

test('mailbox expires after seven days, and the storage alarm deletes its record', async () => {
  const env = mockEnvironment();
  const id = 'aab1cc22-1111-4111-8111-123456789abc';
  const mailbox = env.RESULTS.get(id);
  const originalNow = Date.now;
  Date.now = () => 1700000000000;
  try {
    const created = await mailbox.fetch(new Request('https://results.internal/create', {
      method: 'PUT', body: JSON.stringify({ id, query: 'weather' }),
    }));
    assert.equal(created.status, 201);
    assert.equal(env.alarms.get(id), 1700604800000);
    Date.now = () => 1700604800001;
    assert.equal((await handle(req(`/api/v1/results/${id}`), env)).status, 404);
    await mailbox.alarm();
    assert.equal(env.alarms.has(id), false);
  } finally { Date.now = originalNow; }
});
