import test from 'node:test';
import assert from 'node:assert/strict';
import { handle } from '../worker/index.mjs';
import { keywordScore, cosineSimilarity, searchIndexedAgents } from '../worker/semantic.mjs';

const weather = {
  kind: 'a2a-agent', installable: false, verification: 'card_read_only',
  name: 'Weather Forecaster', description: 'Reports rain and precipitation',
  source_url: 'https://weather.example.org/.well-known/agent-card.json',
  skills: [{ name: 'Meteorology', description: 'Atmospheric conditions', tags: ['climate'] }],
};
const finance = {
  kind: 'a2a-agent', installable: false, verification: 'card_read_only',
  name: 'Market Analyst', description: 'Returns equity prices',
  source_url: 'https://markets.example.org/agent-card.json',
  skills: [{ name: 'Financial reporting', description: 'Stock quotations', tags: ['economics'] }],
};
const privateCard = { ...weather, name: 'Private Network', source_url: 'https://127.0.0.1/private' };
const feed = JSON.stringify({
  schema: 1, source: 'searcha2a', generated_at: '2026-11-12T11:12:20Z',
  listings: [finance, weather, privateCard],
});

const request = (query, options = {}) =>
  new Request('https://searcha2a-api.example.org/api/v1/directory?q=' + encodeURIComponent(query), options);

function withCatalog(t, data = feed) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(data, { headers: { 'content-type': 'application/json' } });
  };
  t.after(() => { globalThis.fetch = original; });
  return calls;
}

test('semantic embeddings rank non-keyword matches without connecting or dispatching', async t => {
  const calls = withCatalog(t);
  const invoked = [];
  const env = {
    SEARCH_RATE_LIMITER: { limit: async ({ key }) => {
      assert.equal(key, 'directory:192.0.2.4');
      return { success: true };
    } },
    AI: { run: async (model, input) => {
      invoked.push({ model, input });
      assert.equal(input.text.length, 3);
      return { data: [[1, 0], [0, 1], [1, 0]] };
    } },
  };
  // No overlap: "sky" and "outlook" never appear in either card's name or skills.
  const response = await handle(request('sky outlook', {
    headers: { Origin: 'https://aive.example.org', 'CF-Connecting-IP': '192.0.2.4' },
  }), env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), '*');
  const result = await response.json();
  assert.equal(result.match_mode, 'semantic');
  assert.equal(result.results[0].name, 'Weather Forecaster');
  assert.equal(result.results.length, 1);
  assert.equal(result.verification, 'card_read_only');
  assert.equal(result.results[0].installable, false);
  assert.equal(result.results[0].match_score, 0.85);
  assert.equal(invoked.length, 1);
  assert.equal(invoked[0].model, '@cf/baai/bge-small-en-v1.5');
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /raw\.githubusercontent\.com/);
  assert.equal(calls[0].options.redirect, 'manual');
});

test('missing embedding entitlement falls back to explicitly labeled keyword matching', async t => {
  withCatalog(t);
  const response = await handle(request('weather'), {
    SEARCH_RATE_LIMITER: { limit: async () => ({ success: true }) },
  });
  const result = await response.json();
  assert.equal(response.status, 200);
  assert.equal(result.match_mode, 'keyword-fallback');
  assert.deepEqual(result.results.map(x => x.name), ['Weather Forecaster']);
});

test('invalid embedding response is not advertised as semantic ranking', async () => {
  const result = await searchIndexedAgents('financial', {
    AI: { run: async () => ({ data: [[1, 0], [Number.NaN, 0], [0, 1]] }) },
  }, async () => new Response(feed));
  assert.equal(result.match_mode, 'keyword-fallback');
  assert.deepEqual(result.results.map(x => x.name), ['Market Analyst']);
});

test('read-only query validates input and applies a per-client rate limit before reading catalog', async t => {
  const calls = withCatalog(t);
  const env = { SEARCH_RATE_LIMITER: { limit: async () => ({ success: false }) } };
  assert.equal((await handle(request('weather'), env)).status, 429);
  assert.equal((await handle(request('x'), env)).status, 400);
  assert.equal((await handle(request('a'.repeat(121)), env)).status, 400);
  assert.equal((await handle(request('weather', { method: 'POST' }), env)).status, 405);
  assert.equal((await handle(request('weather', { method: 'OPTIONS' }), env)).status, 204);
  assert.equal(calls.length, 0);
});

test('feed failures are bounded and do not leak upstream error details', async t => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error('private upstream diagnostics'); };
  t.after(() => { globalThis.fetch = original; });
  const response = await handle(request('weather'), {
    SEARCH_RATE_LIMITER: { limit: async () => ({ success: true }) },
  });
  assert.equal(response.status, 502);
  const result = await response.json();
  assert.match(result.error, /temporarily unavailable/);
  assert.doesNotMatch(result.error, /private/);
});

test('reject malformed catalog and private or credential-bearing Agent Cards', async () => {
  const body = JSON.stringify({ schema: 1, source: 'searcha2a', listings: [
    { ...weather, source_url: 'https://user:pass@weather.example.org/card' },
    { ...weather, source_url: 'http://weather.example.org/card' },
    { ...weather, source_url: 'https://localhost/card' },
    weather,
  ] });
  const result = await searchIndexedAgents('weather', {}, async () => new Response(body));
  assert.equal(result.count, 1);
  assert.deepEqual(result.results.map(x => x.name), ['Weather Forecaster']);
  await assert.rejects(
    searchIndexedAgents('weather', {}, async () => new Response('{"agents":[]}')),
    /Unexpected catalog schema/,
  );
});

test('vector similarity is independent of keyword fallback', () => {
  assert.equal(cosineSimilarity([3, 0], [0, 4]), 0);
  assert.equal(cosineSimilarity([3, 0], [1, 0]), 1);
  assert.equal(cosineSimilarity([0, 0], [1, 0]), null);
  assert.equal(keywordScore('weather', weather), 1);
  assert.equal(keywordScore('weather', finance), 0);
});
