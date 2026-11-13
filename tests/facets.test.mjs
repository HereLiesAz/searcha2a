import test from 'node:test';
import assert from 'node:assert/strict';
import {
  registryEnvironment, card, ingest, reportHealth, getJson,
} from './helpers/registry-fixture.mjs';

const backing = {
  declared_provider: { organization: '10ML', url: 'https://10ml.com', url_matches_endpoint: false,
    source: 'card.provider' },
  hosting: { platform: 'unknown', basis: 'none', host: 'agents.example', shared_platform: false },
  model_provider: null, confidence: 'declared',
  evidence: [{ kind: 'card_field', path: 'provider.organization' }],
  unknowns: ['actual_model', 'subprocessors', 'data_retention'],
};
const access = {
  auth_declared: 'bearer', auth_types: ['bearer'], keyless: 'no',
  payment_required: 'none_declared', evidence: [{ kind: 'card_field', path: 'securitySchemes' }],
  flags: [],
};
const A = 'aaaaaaaaaaaaaaaaaaaa';
const withFacets = (extra = {}) => card(A, 'Weather', 'Forecast', 'Rain', {
  provider: { organization: '10ML', url: 'https://10ml.com' }, backing, access, ...extra,
});

test('ingest keeps additive provider, backing and access and leaves old keys alone', async () => {
  const state = registryEnvironment();
  try {
    assert.equal((await ingest(state.env, [withFacets()])).status, 200);
    const { body } = await getJson(state.env, '/api/v1/agents/' + A);
    assert.deepEqual(body.backing, backing);
    assert.deepEqual(body.access, access);
    assert.equal(body.provider.organization, '10ML');
    assert.equal(body.verification, 'card_read_only');
    assert.equal(body.curation_status, 'unreviewed');
  } finally { state.close(); }
});

test('rows without facets (older crawler) are still accepted', async () => {
  const state = registryEnvironment();
  try {
    assert.equal((await ingest(state.env, [card(A, 'Old', 'Forecast')])).status, 200);
    const { body } = await getJson(state.env, '/api/v1/agents/' + A);
    assert.equal(body.backing, undefined);
  } finally { state.close(); }
});

test('facets present for a card whose raw_card was dropped for size', async () => {
  const state = registryEnvironment();
  try {
    const row = withFacets();
    assert.equal(row.raw_card, undefined);
    await ingest(state.env, [row]);
    const { body } = await getJson(state.env, '/api/v1/agents/' + A);
    assert.equal(body.raw_card, undefined);
    assert.equal(body.access.keyless, 'no');
  } finally { state.close(); }
});

test('malformed or oversized facets reject the whole batch atomically', async () => {
  const state = registryEnvironment();
  const B = 'bbbbbbbbbbbbbbbbbbbb';
  const bad = [
    { access: { ...access, keyless: 'observed' } },
    { access: { ...access, auth_types: ['hacked'] } },
    { access: { ...access, payment_required: 'yes' } },
    { access: 'nope' },
    { backing: { ...backing, confidence: 'certain' } },
    { backing: { ...backing, evidence: Array(50).fill({ kind: 'card_field', path: 'x' }) } },
    { backing: { ...backing, declared_provider: { ...backing.declared_provider,
      organization: 'o'.repeat(500) } } },
    { provider: { organization: 'x'.repeat(500), url: 'https://x.example' } },
    { provider: { organization: 'x', url: 'javascript:alert(1)' } },
  ];
  try {
    for (const extra of bad) {
      const response = await ingest(state.env, [card(B, 'Good', 'Forecast'), withFacets(extra)]);
      assert.equal(response.status, 400, JSON.stringify(extra).slice(0, 80));
    }
    assert.equal((await getJson(state.env, '/api/v1/agents/' + B)).status, 404);
  } finally { state.close(); }
});

test('health: 404 is stored as path_not_found, never as responding', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [card(A, 'Weather', 'Forecast')]);
    const endpoint = 'https://agents.example/a2a';
    const lie = await reportHealth(state.env, [{ id: A, endpoint, status: 'responding',
      state: 'path_not_found', http_status: 404 }]);
    assert.equal(lie.status, 400);
    // An older crawler that still calls 404 "responding" is corrected, not believed.
    assert.equal((await reportHealth(state.env, [{ id: A, endpoint, status: 'responding',
      http_status: 404 }])).status, 200);
    assert.equal((await getJson(state.env, `/api/v1/agents/${A}/health`)).body.status,
      'path_not_found');
    assert.equal((await reportHealth(state.env, [{ id: A, endpoint, status: 'path_not_found',
      state: 'path_not_found', http_status: 404, redirect: false }])).status, 200);
    const { body } = await getJson(state.env, `/api/v1/agents/${A}/health`);
    assert.equal(body.status, 'path_not_found');
    assert.equal(body.state, 'path_not_found');
    assert.equal(body.http_status, 404);
    assert.equal(body.protocol_verified, false);
  } finally { state.close(); }
});

test('health: legacy report without state is classified honestly on read', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [card(A, 'Weather', 'Forecast')]);
    const endpoint = 'https://agents.example/a2a';
    await reportHealth(state.env, [{ id: A, endpoint, status: 'responding', http_status: 405 }]);
    const { body } = await getJson(state.env, `/api/v1/agents/${A}/health`);
    assert.equal(body.status, 'responding');
    assert.equal(body.state, 'reachable');
  } finally { state.close(); }
});

test('health: redirect and challenge facts are bounded and stored', async () => {
  const state = registryEnvironment();
  try {
    await ingest(state.env, [card(A, 'Weather', 'Forecast')]);
    const endpoint = 'https://agents.example/a2a';
    assert.equal((await reportHealth(state.env, [{ id: A, endpoint, status: 'responding',
      state: 'reachable', http_status: 401, redirect: false, challenge: 'bearer',
      tls_not_after: '2027-01-01T00:00:00Z' }])).status, 200);
    assert.equal((await reportHealth(state.env, [{ id: A, endpoint, status: 'responding',
      state: 'reachable', http_status: 401, challenge: '<script>' }])).status, 400);
    const { body } = await getJson(state.env, `/api/v1/agents/${A}/health`);
    assert.equal(body.challenge, 'bearer');
    assert.equal(body.tls_not_after, '2027-01-01T00:00:00Z');
  } finally { state.close(); }
});
