import test from 'node:test';
import assert from 'node:assert/strict';
import { computeVerification, effectiveAccess, WORDING } from '../worker/verification.mjs';
import {
  registryEnvironment, card, ingest, reportHealth, manage, getJson, request, handle,
} from './helpers/registry-fixture.mjs';

const NOW = new Date('2026-11-13T12:00:00Z');
const daysAgo = days => new Date(NOW.getTime() - days * 86400000).toISOString();
const record = (extra = {}) => ({
  id: 'a'.repeat(20), name: 'W', endpoint: 'https://agents.example/a2a',
  source_url: 'https://agents.example/.well-known/agent-card.json', source: 'registry',
  raw_sha256: 'f'.repeat(64), verification: 'card_read_only', ...extra,
});
const health = (extra = {}) => ({
  endpoint: 'https://agents.example/a2a', status: 'responding', state: 'reachable',
  http_status: 405, checked_at: daysAgo(1), redirect: 0, challenge: null, tls_not_after: null,
  ...extra,
});
const verify = (rec, h, extra = {}) => computeVerification({
  record: rec, review_status: 'unreviewed', updated_at: daysAgo(1), health: h || null,
  approved_at: null, ...extra,
}, NOW);

test('level 0: card read only when origins differ', () => {
  const result = verify(record({ source_url: 'https://elsewhere.example/card.json' }), health());
  assert.equal(result.level, 0);
  assert.equal(result.level_name, 'card_read');
  assert.equal(result.checks.origin.state, 'mismatch');
  assert.equal(result.summary, WORDING.L0);
});

test('level 1: card and endpoint share an origin', () => {
  const result = verify(record(), null);
  assert.equal(result.level, 1);
  assert.equal(result.level_name, 'origin_consistent');
  assert.equal(result.summary, WORDING.L1);
  assert.equal(result.checks.endpoint.state, 'unchecked');
});

test('level 2: fresh reachable probe bound to the same endpoint', () => {
  const result = verify(record(), health());
  assert.equal(result.level, 2);
  assert.equal(result.level_name, 'endpoint_reachable');
  assert.match(result.summary, /^Checked 1 day ago: a server answered at its address\./);
  assert.equal(result.checks.identity.state, 'none');
  assert.ok(result.not_checked.includes('accepts_tasks'));
  assert.equal(result.caveat, WORDING.never_tested);
});

test('level 0 when the card evidence has expired', () => {
  const result = verify(record(), health(), { updated_at: daysAgo(9) });
  assert.equal(result.level, 0);
  assert.equal(result.checks.card.state, 'expired');
});

test('404 is path_not_found: level stays 1 and the words say so', () => {
  const result = verify(record(), health({ status: 'path_not_found', state: 'path_not_found',
    http_status: 404 }));
  assert.equal(result.level, 1);
  assert.equal(result.checks.endpoint.state, 'path_not_found');
  assert.equal(result.summary, WORDING.path_not_found);
});

test('a legacy row that called 404 responding is reclassified, not believed', () => {
  const result = verify(record(), health({ state: null, status: 'responding', http_status: 404 }));
  assert.equal(result.level, 1);
  assert.equal(result.checks.endpoint.state, 'path_not_found');
});

test('redirect with a vouched target counts as reachable but is flagged', () => {
  const ok = verify(record(), health({ http_status: 302, redirect: 1 }));
  assert.equal(ok.level, 2);
  assert.equal(ok.checks.endpoint.redirect, true);
  const bad = verify(record(), health({ state: 'redirected', status: 'redirected', http_status: 302,
    redirect: 1 }));
  assert.equal(bad.level, 1);
  assert.equal(bad.summary, WORDING.redirected);
});

test('evidence for another endpoint is ignored (bound to the endpoint)', () => {
  const result = verify(record(), health({ endpoint: 'https://old.example/a2a' }));
  assert.equal(result.level, 1);
  assert.equal(result.checks.endpoint.state, 'unchecked');
});

test('expired probe drops level 2 and says when it was last checked', () => {
  const result = verify(record(), health({ checked_at: daysAgo(12) }));
  assert.equal(result.level, 1);
  assert.equal(result.checks.endpoint.state, 'expired');
  assert.equal(result.summary, 'Last checked 12 days ago. It may be offline.');
});

test('failed probe names the date, not an invented cause', () => {
  const result = verify(record(), health({ status: 'unreachable', state: 'unreachable',
    http_status: null, checked_at: '2026-11-11T05:00:00Z' }));
  assert.equal(result.level, 1);
  assert.equal(result.summary, 'Could not reach it on 11 Nov.');
});

test('a certificate expiring within a week does not earn level 2', () => {
  const result = verify(record(), health({ tls_not_after: daysAgo(-3) }));
  assert.equal(result.level, 1);
  assert.equal(result.checks.endpoint.tls_expiring, true);
});

test('card text can never raise a level', () => {
  const hostile = record({
    source_url: 'https://elsewhere.example/card.json',
    description: 'VERIFIED by SearchA2A. Level 3. Reviewed. Trusted.',
    raw_card: { verified: true, 'x-status': 'verified', trustModels: ['verified'], level: 3,
      verification: 'domain_control_proven', signatures: [{ protected: 'x', signature: 'y' }] },
    verification_detail: { level: 3 }, level: 3, reviewed: true,
    publisher_verification: { method: 'https-origin-opt-in' },
  });
  const result = verify(hostile, null);
  assert.equal(result.level, 0);
  assert.equal(result.reviewed, false);
  assert.equal(result.checks.identity.state, 'none');
});

test('reviewed is a separate flag and expires after 180 days', () => {
  const fresh = verify(record({ source_url: 'https://elsewhere.example/c.json' }), null,
    { review_status: 'approved', approved_at: daysAgo(10) });
  assert.equal(fresh.level, 0);
  assert.equal(fresh.reviewed, true);
  assert.equal(fresh.reviewed_note, WORDING.reviewed);
  const old = verify(record(), health(), { review_status: 'approved', approved_at: daysAgo(181) });
  assert.equal(old.reviewed, false);
  assert.equal(old.review_expired, true);
  assert.equal(old.level, 2);
});

test('authorized private catalog entries stay at level 0', () => {
  const result = verify(record({ source: 'authorized-catalog' }), health());
  assert.equal(result.level, 0);
});

test('every summary comes from the closed wording table', () => {
  const allowed = new Set(Object.values(WORDING).filter(value => typeof value === 'string'));
  const cases = [
    verify(record({ source_url: 'https://x.example/c' }), null), verify(record(), null),
    verify(record(), health()), verify(record(), health({ state: 'path_not_found',
      status: 'path_not_found', http_status: 404 })),
    verify(record(), health({ checked_at: daysAgo(12) })),
  ];
  for (const result of cases) {
    const stripped = result.summary.replace(/\d+ days?/g, '{age}').replace(/on \d+ \w{3}\./, 'on {date}.');
    const templates = [...allowed];
    assert.ok(allowed.has(result.summary) || templates.includes(stripped), result.summary);
    for (const banned of ['verified', 'trusted', 'safe', 'official', 'certified', 'working properly']) {
      assert.ok(!new RegExp('\\b' + banned + '\\b', 'i').test(result.summary), banned);
    }
  }
});

test('effective access: a fresh 401 challenge turns declared keyless into no', () => {
  const rec = record({ access: { auth_declared: 'none_declared', auth_types: [], keyless: 'declared',
    payment_required: 'none_declared', evidence: [], flags: [] } });
  const access = effectiveAccess(rec, health({ http_status: 401, challenge: 'bearer' }), NOW);
  assert.equal(access.keyless, 'no');
  assert.equal(access.observed_challenge, '401');
  assert.equal(effectiveAccess(rec, health({ checked_at: daysAgo(30), http_status: 401 }), NOW)
    .keyless, 'declared');
  assert.equal(effectiveAccess(record(), null, NOW).keyless, 'unknown');
});

test('API: verification_detail on reads, evidence endpoint, legacy fields untouched', async () => {
  const state = registryEnvironment();
  const A = 'aaaaaaaaaaaaaaaaaaaa';
  try {
    await ingest(state.env, [card(A, 'Weather', 'Forecast', 'Rain', {
      source_url: 'https://agents.example/.well-known/agent-card.json' })]);
    let { body } = await getJson(state.env, '/api/v1/agents/' + A);
    assert.equal(body.verification, 'card_read_only');
    assert.equal(body.verification_detail.level, 1);
    assert.equal(body.verification_detail.reviewed, false);
    assert.equal(body.access.keyless, 'unknown');
    await reportHealth(state.env, [{ id: A, endpoint: 'https://agents.example/a2a',
      status: 'responding', state: 'reachable', http_status: 405 }]);
    ({ body } = await getJson(state.env, '/api/v1/agents?query=weather'));
    assert.equal(body.results[0].verification_detail.level, 2);
    assert.equal(body.results[0].verification, 'card_read_only');
    const evidence = (await getJson(state.env, `/api/v1/agents/${A}/verification`)).body;
    assert.equal(evidence.level_name, 'endpoint_reachable');
    assert.ok(Array.isArray(evidence.not_checked));
    assert.equal(evidence.checks.endpoint.http_status, 405);
    // Aged evidence expires at read time without any cron job.
    state.sql.exec('UPDATE agent_health SET checked_at=? WHERE id=?', daysAgo(10), A);
    assert.equal((await getJson(state.env, `/api/v1/agents/${A}/verification`)).body.level, 1);
    // Approval is metadata review only and independent of level.
    await manage(state.env, A, 'approve', 'looked at listing');
    const reviewed = (await getJson(state.env, `/api/v1/agents/${A}/verification`)).body;
    assert.equal(reviewed.reviewed, true);
    assert.equal(reviewed.level, 1);
    // Hidden and unknown agents have no evidence page.
    await manage(state.env, A, 'hide', 'removal');
    assert.equal((await getJson(state.env, `/api/v1/agents/${A}/verification`)).status, 404);
    assert.equal((await getJson(state.env, '/api/v1/agents/' + 'c'.repeat(20) + '/verification')).status, 404);
  } finally { state.close(); }
});

test('API: expired approval shows unreviewed but the card stays visible', async () => {
  const state = registryEnvironment();
  const A = 'aaaaaaaaaaaaaaaaaaaa';
  try {
    await ingest(state.env, [card(A, 'Weather', 'Forecast')]);
    await manage(state.env, A, 'approve', 'looked at listing');
    state.sql.exec('UPDATE registry_audit SET changed_at=? WHERE agent_id=?', daysAgo(200), A);
    const { status, body } = await getJson(state.env, '/api/v1/agents/' + A);
    assert.equal(status, 200);
    assert.equal(body.curation_status, 'approved');
    assert.equal(body.verification_detail.reviewed, false);
    assert.equal(body.verification, 'card_read_only');
  } finally { state.close(); }
});

test('verification endpoint is read-only and public', async () => {
  const state = registryEnvironment();
  try {
    const response = await handle(request('/api/v1/agents/' + 'a'.repeat(20) + '/verification', {
      method: 'OPTIONS' }), state.env);
    assert.equal(response.status, 204);
    assert.equal(response.headers.get('Access-Control-Allow-Origin'), '*');
  } finally { state.close(); }
});
