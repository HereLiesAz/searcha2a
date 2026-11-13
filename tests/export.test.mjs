import test from 'node:test';
import assert from 'node:assert/strict';
import {
  registryEnvironment, card, ingest, reportHealth, manage, getJson, request, handle,
} from './helpers/registry-fixture.mjs';

const A = 'aaaaaaaaaaaaaaaaaaaa';
const B = 'bbbbbbbbbbbbbbbbbbbb';
const exportEvidence = (env, auth = 'Bearer pipeline-secret') =>
  handle(request('/internal/registry/evidence-export', { headers: { Authorization: auth } }), env);

test('evidence export is authenticated and lists visible agents only', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [
      card(A, 'One', 'Forecast', 'd', { source_url: 'https://agents.example/c.json' }),
      card(B, 'Two', 'Notes'),
    ]);
    await reportHealth(state.env, [{ id: A, endpoint: 'https://agents.example/a2a', status: 'responding',
      state: 'reachable', http_status: 401, challenge: 'bearer' }]);
    assert.equal((await exportEvidence(state.env, 'Bearer wrong')).status, 401);
    assert.equal((await handle(request('/internal/registry/evidence-export'), state.env)).status, 401);
    // The admin credential is not the crawler credential.
    assert.equal((await exportEvidence(state.env, 'Bearer admin-only-secret')).status, 401);
    let body = await (await exportEvidence(state.env)).json();
    const byId = Object.fromEntries(body.entries.map(entry => [entry.id, entry]));
    assert.equal(byId[A].verification_detail.level, 2);
    assert.equal(byId[A].access.observed_challenge, '401');
    assert.equal(byId[A].access.keyless, 'no');
    assert.equal(byId[B].verification_detail.level, 1);
    assert.ok(body.generated_at);
    await manage(state.env, A, 'hide');
    body = await (await exportEvidence(state.env)).json();
    assert.deepEqual(body.entries.map(entry => entry.id), [B]);
  } finally { state.close(); }
});

test('public providers route and CORS', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [card(A, 'One', 'Forecast', 'd', {
      backing: { declared_provider: { organization: 'Org', url: 'https://org.example',
        url_matches_endpoint: false, source: 'card.provider' },
      hosting: { platform: 'unknown', basis: 'none', host: 'agents.example', shared_platform: false },
      model_provider: null, confidence: 'declared', evidence: [], unknowns: [] } })]);
    const { status, body, headers } = await getJson(state.env, '/api/v1/providers?kind=declared_provider');
    assert.equal(status, 200);
    assert.equal(body.sources[0].name, 'Org');
    assert.equal(headers.get('Access-Control-Allow-Origin'), '*');
    assert.equal((await getJson(state.env, '/api/v1/providers?kind=nope')).status, 400);
    const preflight = await handle(request('/api/v1/providers', { method: 'OPTIONS' }), state.env);
    assert.equal(preflight.status, 204);
  } finally { state.close(); }
});

test('existing API response keys are unchanged; new keys are only added', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [card(A, 'One', 'Forecast')]);
    const { body } = await getJson(state.env, '/api/v1/agents?query=one');
    for (const key of ['query', 'count', 'total', 'offset', 'limit', 'generated_at', 'results', 'verified']) {
      assert.ok(key in body, key);
    }
    const row = body.results[0];
    for (const key of ['id', 'name', 'description', 'endpoint', 'source_url', 'skills', 'protocols',
        'capabilities', 'security_schemes', 'version', 'verification', 'curation_status']) {
      assert.ok(key in row, key);
    }
    assert.equal(row.verification, 'card_read_only');
    const health = (await getJson(state.env, `/api/v1/agents/${A}/health`)).body;
    for (const key of ['id', 'status', 'checked_at', 'protocol_verified']) assert.ok(key in health, key);
  } finally { state.close(); }
});
