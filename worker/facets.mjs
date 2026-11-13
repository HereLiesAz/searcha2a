/**
 * Validation and small shared vocabularies for the additive provider/access/health facts.
 * The crawler is trusted to hold the ingest credential, but card content is not:
 * every shape is re-checked and bounded here and none of it is ever executed.
 */
export const AUTH_TYPES = ['apiKey', 'basic', 'bearer', 'mtls', 'oauth', 'openid', 'other', 'x402'];
const AUTH_DECLARED = new Set(['none_declared', 'multiple', 'unknown', ...AUTH_TYPES]);
const KEYLESS = new Set(['declared', 'no', 'unknown']); // never "observed"
const PAYMENT = new Set(['declared', 'none_declared', 'unknown']);
const CONFIDENCE = new Set(['declared', 'unknown']);
const MAX_FACET_BYTES = 2048;

/** Honest endpoint states and the legacy `status` each one maps to. */
export const HEALTH_STATE_STATUS = {
  reachable: 'responding', server_error: 'server_error', path_not_found: 'path_not_found',
  redirected: 'redirected', unexpected_status: 'unexpected_status',
  unreachable: 'unreachable', tls_error: 'unreachable', skipped: 'skipped',
};
export const HEALTH_STATUSES = new Set(Object.values(HEALTH_STATE_STATUS));
const REACHABLE_CODES = new Set([200, 201, 202, 204, 400, 401, 402, 403, 405, 415, 429]);
export const CHALLENGES = new Set(['bearer', 'basic', 'digest', 'negotiate', 'other', 'none']);

/** Same mapping as src/agent_health.py. A 3xx counts only when the crawler vouched for the target. */
export function stateFromHttp(code, { redirectTargetValid = false } = {}) {
  if (!Number.isInteger(code)) return null;
  if (code >= 300 && code < 400) return redirectTargetValid ? 'reachable' : 'redirected';
  if (code >= 500) return 'server_error';
  if (code === 404 || code === 410) return 'path_not_found';
  return REACHABLE_CODES.has(code) ? 'reachable' : 'unexpected_status';
}

const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value, max) => typeof value === 'string' && value.length <= max && !/[\x00-\x1f]/.test(value);
const evidenceList = value => Array.isArray(value) && value.length <= 6 && value.every(item =>
  isObject(item) && text(item.kind, 40) &&
  Object.entries(item).every(([key, val]) => text(key, 40) && text(val, 200)));

function validProviderLike(value, { allowEmptyUrl = true } = {}) {
  return isObject(value) && text(value.organization, 160) && value.organization.trim() !== '' &&
    text(value.url, 300) && (value.url === '' ? allowEmptyUrl : value.url.startsWith('https://'));
}

export function validBacking(value) {
  if (!isObject(value)) return false;
  const declared = value.declared_provider;
  if (declared !== null && !(validProviderLike(declared) &&
      typeof declared.url_matches_endpoint === 'boolean' && text(declared.source, 40))) return false;
  const hosting = value.hosting;
  if (!isObject(hosting) || !text(hosting.platform, 40) || !text(hosting.basis, 40) ||
      !text(hosting.host, 253) || typeof hosting.shared_platform !== 'boolean') return false;
  const model = value.model_provider;
  if (model !== null && !(isObject(model) && text(model.name, 80) && text(model.model, 80) &&
      text(model.basis, 40) && CONFIDENCE.has(model.confidence))) return false;
  return CONFIDENCE.has(value.confidence) && evidenceList(value.evidence) &&
    Array.isArray(value.unknowns) && value.unknowns.length <= 6 &&
    value.unknowns.every(item => text(item, 40)) &&
    JSON.stringify(value).length <= MAX_FACET_BYTES;
}

export function validAccess(value) {
  if (!isObject(value)) return false;
  return AUTH_DECLARED.has(value.auth_declared) && Array.isArray(value.auth_types) &&
    value.auth_types.length <= AUTH_TYPES.length &&
    value.auth_types.every(type => AUTH_TYPES.includes(type)) &&
    KEYLESS.has(value.keyless) && PAYMENT.has(value.payment_required) &&
    evidenceList(value.evidence) && Array.isArray(value.flags) && value.flags.length <= 4 &&
    value.flags.every(flag => text(flag, 60)) && JSON.stringify(value).length <= MAX_FACET_BYTES;
}

/** True when the optional additive facts on an ingest row are absent or well formed. */
export function validFacets(agent) {
  if (agent.provider !== undefined && !validProviderLike(agent.provider)) return false;
  if (agent.backing !== undefined && !validBacking(agent.backing)) return false;
  if (agent.access !== undefined && !validAccess(agent.access)) return false;
  return true;
}
