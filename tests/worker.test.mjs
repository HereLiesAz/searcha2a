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
    SEARCH_RATE_LIMITER: { limit: async () => ({ success: true }) },
    GITHUB_DISPATCH_TOKEN: 'fake', TURNSTILE_SECRET: 'fake', RESULT_CALLBACK_SECRET: 'secret',
  };
}

const req = (url, options = {}) => new Request('https://searcha2a-api.example.com' + url,
  { ...options, headers: { Origin: 'https://searcha2a.azphalt.store', ...(options.headers || {}) } });

test('public search requires Turnstile and does not invoke Actions without it', async () => {
  const env = mockEnvironment();
  const response = await handle(req('/api/v1/search', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: 'weather' }) }), env);
  assert.equal(response.status, 403);
  assert.equal((await response.json()).error, 'Challenge verification failed');
});

test('invalid query never dispatches', async () => {
  const env = mockEnvironment();
  const response = await handle(req('/api/v1/search', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: 'a' }) }), env);
  assert.equal(response.status, 400);
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

test('validated search dispatches a workflow and returns an ID', async () => {
  const originalFetch = globalThis.fetch;
  const env = mockEnvironment();
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (url.includes('turnstile')) return new Response(JSON.stringify({ success: true }), { status: 200 });
    if (url.includes('api.github.com')) return new Response(JSON.stringify({ workflow_run_id: 123 }), { status: 200 });
    throw new Error('Unexpected network call');
  };
  try {
    const response = await handle(req('/api/v1/search', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: 'weather forecast', turnstile: 'valid' }) }), env);
    assert.equal(response.status, 202);
    const result = await response.json();
    assert.equal(result.workflow_run_id, 123);
    assert.match(result.id, /^[0-9a-f-]{36}$/);
    assert.equal(calls.length, 2);
    const payload = JSON.parse(calls[1].options.body);
    assert.equal(payload.inputs.query, 'weather forecast');
    assert.equal(payload.inputs.request_id, result.id);
    const status = await handle(req(`/api/v1/results/${result.id}`), env);
    assert.equal((await status.json()).status, 'queued');
  } finally { globalThis.fetch = originalFetch; }
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

test('fast callback completion survives late dispatch metadata', async () => {
  const env = mockEnvironment();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (url.includes('turnstile')) return new Response('{"success":true}');
    if (url.includes('api.github.com')) {
      const { inputs } = JSON.parse(options.body);
      const callback = await handle(req('/internal/results', {
        method: 'POST', headers: { Authorization: 'Bearer secret' },
        body: JSON.stringify({ request_id: inputs.request_id, query: inputs.query, results: [{ name: 'Fast agent' }] }),
      }), env);
      assert.equal(callback.status, 200);
      return new Response('{"workflow_run_id":123}');
    }
    throw new Error('Unexpected network request');
  };
  try {
    const response = await handle(req('/api/v1/search', {
      method: 'POST', body: JSON.stringify({ query: 'weather', turnstile: 'valid' }),
    }), env);
    assert.equal(response.status, 202);
    const queued = await response.json();
    const result = await handle(req(`/api/v1/results/${queued.id}`), env);
    assert.equal(result.status, 200);
    const body = await result.json();
    assert.equal(body.status, 'complete');
    assert.equal(body.results[0].name, 'Fast agent');
  } finally { globalThis.fetch = originalFetch; }
});

test('failed dispatch removes the mailbox instead of leaving an unfinishable search', async () => {
  const env = mockEnvironment();
  const originalFetch = globalThis.fetch;
  let requestId;
  globalThis.fetch = async (url, options) => {
    if (url.includes('turnstile')) return new Response('{"success":true}');
    requestId = JSON.parse(options.body).inputs.request_id;
    return new Response('{"message":"Denied"}', { status: 403 });
  };
  try {
    const response = await handle(req('/api/v1/search', {
      method: 'POST', body: JSON.stringify({ query: 'weather', turnstile: 'valid' }),
    }), env);
    assert.equal(response.status, 502);
    assert.match((await response.json()).error, /403/);
    assert.equal((await handle(req(`/api/v1/results/${requestId}`), env)).status, 404);
    assert.equal(env.alarms.has(requestId), false);
  } finally { globalThis.fetch = originalFetch; }
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
