import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeAnsName, parseAnsClaim } from '../worker/ans.mjs';

test('publisher-defined versions preserve case, punctuation and numeric segments', () => {
  assert.deepEqual(parseAnsClaim('ans://1.0.0.weather.example.com', 'weather.example.com'), {
    name: 'ans://1.0.0.weather.example.com', agent_host: 'weather.example.com',
    version: '1.0.0', verification: 'unverified',
  });
  assert.equal(parseAnsClaim('ans://release-2026-11.weather.example.com', 'weather.example.com').version,
    'release-2026-11');
  assert.equal(parseAnsClaim('ans://build.27-beta.weather.example.com', 'weather.example.com').version,
    'build.27-beta');
  assert.equal(parseAnsClaim('ans://V3.weather.example.com', 'weather.example.com').version, 'V3');
});

test('ANS claims require a valid explicitly matched hostname', () => {
  assert.equal(parseAnsClaim('ans://release.attacker.example', 'weather.example.com'), null);
  assert.equal(parseAnsClaim('ans://release.weather.example.com', ''), null);
  assert.equal(parseAnsClaim('ans://release.weather.local', 'weather.local'), null);
  assert.equal(parseAnsClaim('ans://release.weather.example.com/path', 'weather.example.com'), null);
  assert.equal(parseAnsClaim('https://release.weather.example.com', 'weather.example.com'), null);
  assert.equal(normalizeAnsName('ans://1.0.0.weather.example.com'), 'ans://1.0.0.weather.example.com');
  assert.equal(normalizeAnsName('ans://-bad.weather.example.com'), null);
});
