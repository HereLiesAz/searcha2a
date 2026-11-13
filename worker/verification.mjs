/**
 * Verification levels, computed at read time from evidence rows with expiry.
 *
 * Rules that must hold (they are enforced by tests/verification.test.mjs):
 *  - A level never rises because of anything the card says about itself. Only structured
 *    registry fields (source_url, endpoint, probe results from the Actions runner) are read.
 *  - Missing, expired or failed evidence lowers the level. Unknown is never "ok".
 *  - Evidence is bound to the current endpoint and card hash.
 *  - Nothing here says an agent works, is safe, or who owns it. Wording is a closed table.
 *
 * L3 (domain control proven) is deliberately NOT implemented: it needs a DNSSEC-validated
 * TXT lookup or a JWS check that cannot be done cheaply and safely inside this Worker. The
 * `identity` check is always `none` and the level never exceeds 2. See docs/API.md.
 */
import { HEALTH_STATE_STATUS, stateFromHttp } from './facets.mjs';

const DAY = 86400000;
export const EVIDENCE_TTL_MS = 7 * DAY;
export const REVIEW_TTL_MS = 180 * DAY;
const MIN_CERT_LIFE_MS = 7 * DAY;
export const LEVEL_NAMES = ['card_read', 'origin_consistent', 'endpoint_reachable', 'domain_control_proven'];
export const NOT_CHECKED = ['accepts_tasks', 'speaks_a2a', 'correct_output', 'safe',
  'legal_identity', 'pricing_accuracy'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** The only sentences a person is shown about verification. Never built from card text. */
export const WORDING = {
  L0: 'Listed only. Nobody has checked this agent.',
  L1: 'Its listing was published on the same website as the service.',
  L2: 'Checked {age} ago: a server answered at its address. That does not mean it can do your job.',
  L3: "The publisher showed they control this website's domain. That is not a check of who they are.",
  reviewed: 'A SearchA2A reviewer looked at the listing. They did not test the agent.',
  expired: 'Last checked {age} ago. It may be offline.',
  unreachable: 'Could not reach it on {date}.',
  path_not_found: "A server answered, but not at the agent's address. It may not be working.",
  redirected: "Its address pointed somewhere else when it was last checked.",
  server_error: 'Its server reported an error when it was last checked.',
  never_tested: 'No one has tested what this agent does.',
};

const plural = (n, unit) => `${n} ${unit}${n === 1 ? '' : 's'}`;
const ageText = (ms) => {
  const days = Math.floor(ms / DAY);
  return days < 1 ? 'less than a day' : plural(days, 'day');
};
const dateText = iso => {
  const t = new Date(iso);
  return `${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`;
};
const fill = (template, values) => template.replace(/\{(\w+)\}/g, (_, key) => values[key]);
const stamp = ms => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

function originOf(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return null;
    return `https://${parsed.hostname}${parsed.port && parsed.port !== '443' ? ':' + parsed.port : ''}`;
  } catch { return null; }
}

/** Honest endpoint state for a stored health row; rows written before the taxonomy are reclassified. */
export function endpointState(row) {
  if (row.state && HEALTH_STATE_STATUS[row.state]) return row.state;
  if (row.http_status === null || row.http_status === undefined) {
    return row.status === 'skipped' ? 'skipped' : 'unreachable';
  }
  return stateFromHttp(row.http_status);
}

/**
 * input: { record, review_status, updated_at, health|null, approved_at|null }
 * Pure function of its input and `now`.
 */
export function computeVerification(input, now = new Date()) {
  const { record, review_status: reviewStatus } = input;
  const nowMs = now.getTime();
  const privateCatalog = record.source === 'authorized-catalog';

  // L0: the card was read and is still fresh.
  const updatedMs = Date.parse(input.updated_at);
  const cardFresh = Number.isFinite(updatedMs) && nowMs - updatedMs <= EVIDENCE_TTL_MS &&
    updatedMs <= nowMs + 60000;
  const card = {
    state: cardFresh ? 'ok' : 'expired',
    checked_at: Number.isFinite(updatedMs) ? stamp(updatedMs) : null,
    expires_at: Number.isFinite(updatedMs) ? stamp(updatedMs + EVIDENCE_TTL_MS) : null,
    card_sha256: typeof record.raw_sha256 === 'string' ? record.raw_sha256 : null,
    source_url: typeof record.source_url === 'string' ? record.source_url.slice(0, 300) : null,
  };

  // L1: the card and the endpoint share a scheme, host and port. Computed from structured
  // fields only, so it is always bound to the current endpoint and card.
  const cardOrigin = originOf(record.source_url);
  const endpointOrigin = originOf(record.endpoint);
  let originState = 'unknown';
  if (privateCatalog) originState = 'skipped';
  else if (cardOrigin && endpointOrigin) originState = cardOrigin === endpointOrigin ? 'match' : 'mismatch';
  const hosting = record.backing?.hosting;
  const origin = {
    state: originState, card_origin: cardOrigin, endpoint_origin: endpointOrigin,
    shared_platform: hosting?.shared_platform ? hosting.platform : null,
    consent_proof: record.publisher_verification?.method === 'https-origin-opt-in',
    checked_at: card.checked_at, expires_at: card.expires_at,
  };

  // L2: a fresh, credential-free probe of this exact endpoint found a server there.
  const row = input.health;
  let endpoint = { state: 'unchecked' };
  let endpointFresh = false;
  if (row && !privateCatalog && row.endpoint === record.endpoint) {
    const checkedMs = Date.parse(row.checked_at);
    const state = endpointState(row);
    const expired = !Number.isFinite(checkedMs) || nowMs - checkedMs > EVIDENCE_TTL_MS;
    const notAfter = row.tls_not_after ? Date.parse(row.tls_not_after) : null;
    const tlsExpiring = notAfter !== null && Number.isFinite(notAfter) &&
      notAfter - checkedMs < MIN_CERT_LIFE_MS;
    endpoint = {
      state: expired ? 'expired' : state, last_state: state,
      method: 'HEAD', http_status: row.http_status ?? null, redirect: row.redirect === 1,
      ...(row.challenge ? { challenge: row.challenge } : {}),
      ...(row.tls_not_after ? { tls_not_after: row.tls_not_after, tls_expiring: tlsExpiring } : {}),
      checked_at: Number.isFinite(checkedMs) ? stamp(checkedMs) : null,
      expires_at: Number.isFinite(checkedMs) ? stamp(checkedMs + EVIDENCE_TTL_MS) : null,
      probe_version: 2,
    };
    endpointFresh = !expired && state === 'reachable' && !tlsExpiring;
  }

  const identity = { state: 'none' };
  let level = 0;
  if (card.state === 'ok' && !privateCatalog) {
    level = 0;
    if (origin.state === 'match') {
      level = 1;
      if (endpointFresh) level = 2;
    }
  }

  // Review is a separate axis, with its own expiry; an expired approval never re-hides a card.
  const approvedMs = input.approved_at ? Date.parse(input.approved_at) : null;
  const approved = reviewStatus === 'approved';
  const reviewExpired = approved && approvedMs !== null && Number.isFinite(approvedMs) &&
    nowMs - approvedMs > REVIEW_TTL_MS;
  const reviewed = approved && !reviewExpired;

  // Wording: a closed table keyed by level and the latest honest evidence state.
  let summary = WORDING['L' + level];
  if (level === 2) summary = fill(WORDING.L2, { age: ageText(nowMs - Date.parse(endpoint.checked_at)) });
  else if (endpoint.state === 'expired') {
    summary = fill(WORDING.expired, { age: ageText(nowMs - Date.parse(endpoint.checked_at)) });
  } else if (['unreachable', 'tls_error'].includes(endpoint.state)) {
    summary = fill(WORDING.unreachable, { date: dateText(endpoint.checked_at) });
  } else if (['path_not_found', 'redirected', 'server_error'].includes(endpoint.state)) {
    summary = WORDING[endpoint.state];
  }

  return {
    id: record.id, level, level_name: LEVEL_NAMES[level], reviewed,
    ...(reviewExpired ? { review_expired: true } : {}),
    computed_at: stamp(nowMs),
    checks: { card, origin, endpoint, identity },
    summary, ...(reviewed ? { reviewed_note: WORDING.reviewed } : {}),
    caveat: WORDING.never_tested,
    not_checked: NOT_CHECKED,
  };
}

/** The compact form attached to list rows and exports. */
export function verificationDetail(full) {
  const checked = full.checks.endpoint.checked_at || full.checks.card.checked_at;
  const expires = full.level >= 2 ? full.checks.endpoint.expires_at : full.checks.card.expires_at;
  return {
    level: full.level, level_name: full.level_name, reviewed: full.reviewed,
    computed_at: full.computed_at, checked_at: checked, expires_at: expires, summary: full.summary,
  };
}

const NO_ACCESS = { auth_declared: 'unknown', auth_types: [], keyless: 'unknown',
  payment_required: 'unknown', evidence: [], flags: [] };

/**
 * What the card declares about keys and payment, plus any fresh negative evidence.
 * keyless stays declared|no|unknown: it is never "observed", because proving an agent
 * needs no key would require sending it a task.
 */
export function effectiveAccess(record, health, now = new Date()) {
  const access = record.access && typeof record.access === 'object' ? { ...record.access } : { ...NO_ACCESS };
  access.observed_challenge = null;
  if (health && health.endpoint === record.endpoint && [401, 402, 403].includes(health.http_status)) {
    const checked = Date.parse(health.checked_at);
    if (Number.isFinite(checked) && now.getTime() - checked <= EVIDENCE_TTL_MS) {
      access.observed_challenge = String(health.http_status);
      access.keyless = 'no';
    }
  }
  return access;
}
