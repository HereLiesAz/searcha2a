import test from 'node:test';
import assert from 'node:assert/strict';
import { handle } from '../worker/index.mjs';

function mockEnvironment() {
  const map = new Map();
  return {
    ALLOWED_ORIGIN: 'https://searcha2a.azphalt.store',
    RESULTS: { get: async key => map.get(key) || null, put: async (key, value) => map.set(key, value), delete: async key => map.delete(key) },
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
  await env.RESULTS.put(`search:${id}`, JSON.stringify({ id, query: 'weather', status: 'queued' }));
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
