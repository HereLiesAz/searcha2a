import test from 'node:test';
import assert from 'node:assert/strict';
import { indexedMatches, mergeResults } from '../site/search-results.mjs';

const cards = [
  { id: 'weather', name: 'Cloud Observer', description: 'Reports rainfall', source: 'registry',
    skills: [{ name: 'Forecast', description: 'Weather data', tags: ['temperature'] }] },
  { id: 'code', name: 'Code Reviewer', description: 'Finds security issues',
    skills: [{ name: 'Review', description: 'Source changes', tags: ['lint'] }] },
  { id: 'other', name: 'Market Researcher', description: 'Tracks weather impacts',
    skills: [{ name: 'Research', description: 'Reports', tags: [] }] },
];

test('published registry cards match by name, skill, and description before dispatch', () => {
  assert.deepEqual(indexedMatches(cards, 'weather').map(x => x.id), ['weather', 'other']);
  assert.deepEqual(indexedMatches(cards, 'review').map(x => x.id), ['code']);
  assert.deepEqual(indexedMatches(cards, 'rainfall').map(x => x.id), ['weather']);
});

test('search is case insensitive and empty/unrelated queries do not fabricate matches', () => {
  assert.deepEqual(indexedMatches(cards, 'WEATHER').map(x => x.id), ['weather', 'other']);
  assert.deepEqual(indexedMatches(cards, 'xyzzy').map(x => x.id), []);
  assert.deepEqual(indexedMatches(cards, '  '), []);
  assert.deepEqual(indexedMatches(null, 'weather'), []);
});

test('live completion appends additional agents without losing indexed matches', () => {
  const live = [{ id: 'new', name: 'Sky Watch' }];
  const indexed = [cards[0]];
  assert.deepEqual(mergeResults(indexed, live).map(x => x.id), ['weather', 'new']);
  assert.deepEqual(mergeResults(indexed, []).map(x => x.id), ['weather']);
});

test('live version replaces duplicate indexed card without adding duplicate results', () => {
  const live = [{ id: 'weather', name: 'Updated Cloud Observer' }, { id: 'new', name: 'Sky Watch' }];
  const merged = mergeResults([cards[0]], live);
  assert.equal(merged.length, 2);
  assert.equal(merged[0].name, 'Updated Cloud Observer');
  assert.equal(merged[1].name, 'Sky Watch');
});

test('records without IDs can still be deduplicated by source URL', () => {
  const local = [{ name: 'Original', source_url: 'https://agents.example/card.json' }];
  const live = [{ name: 'Updated', source_url: 'https://agents.example/card.json' }];
  assert.deepEqual(mergeResults(local, live), live);
});
