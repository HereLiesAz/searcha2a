/** Filters shared by GET /api/v1/agents and the MCP search_agents tool. Pure functions. */
const SORTS = new Set(['relevance', 'level', 'keyless_first', 'recently_checked']);
const PROTOCOLS = new Set(['JSONRPC', 'HTTP+JSON', 'GRPC']);
const DAY = 86400000;
export const MAX_FILTER_SCAN = 2000;

const noControls = value => !/[\x00-\x1f\x7f]/.test(value);

/** Returns { filters } or { error }. Every value is validated; nothing is interpolated into SQL. */
export function parseFilters(params) {
  const get = name => {
    const value = params.get(name);
    return value === null || value === '' ? null : value;
  };
  const filters = { computed: false };
  const keyless = get('keyless');
  if (keyless !== null && keyless !== 'any') {
    if (!['declared', 'no', 'unknown'].includes(keyless)) return { error: 'Invalid keyless filter' };
    filters.keyless = keyless;
  }
  const payment = get('payment');
  if (payment !== null && payment !== 'any') {
    if (!['declared', 'none_declared', 'unknown'].includes(payment)) return { error: 'Invalid payment filter' };
    filters.payment = payment;
  }
  const minLevel = get('min_level');
  if (minLevel !== null) {
    if (!/^[0-3]$/.test(minLevel)) return { error: 'Invalid min_level' };
    filters.minLevel = Number(minLevel);
  }
  const reviewed = get('reviewed');
  if (reviewed !== null) {
    if (!['true', 'false'].includes(reviewed)) return { error: 'Invalid reviewed filter' };
    filters.reviewed = reviewed === 'true';
  }
  const provider = get('provider');
  if (provider !== null) {
    if (provider.length > 80 || !noControls(provider)) return { error: 'Invalid provider filter' };
    filters.provider = provider.toLowerCase();
  }
  const hosting = get('hosting');
  if (hosting !== null) {
    if (!/^[a-z0-9-]{1,40}$/.test(hosting)) return { error: 'Invalid hosting filter' };
    filters.hosting = hosting;
  }
  const protocol = get('protocol');
  if (protocol !== null) {
    if (!PROTOCOLS.has(protocol.toUpperCase())) return { error: 'Invalid protocol filter' };
    filters.protocol = protocol.toUpperCase();
  }
  const skill = get('skill');
  if (skill !== null) {
    if (skill.length > 60 || !noControls(skill)) return { error: 'Invalid skill filter' };
    filters.skill = skill.toLowerCase();
  }
  const fresh = get('fresh_within_days');
  if (fresh !== null) {
    if (!/^\d{1,2}$/.test(fresh) || Number(fresh) < 1 || Number(fresh) > 30) {
      return { error: 'Invalid fresh_within_days' };
    }
    filters.freshDays = Number(fresh);
  }
  const sort = get('sort') || 'relevance';
  if (!SORTS.has(sort)) return { error: 'Invalid sort' };
  filters.sort = sort;
  filters.computed = ['keyless', 'payment', 'minLevel', 'reviewed', 'provider', 'hosting',
    'protocol', 'freshDays'].some(key => filters[key] !== undefined) || sort !== 'relevance';
  return { filters };
}

/** item: the public agent row (with access and verification_detail); full: verification evidence. */
export function matchesFilters(item, full, filters, now = new Date()) {
  if (filters.keyless && item.access?.keyless !== filters.keyless) return false;
  if (filters.payment && item.access?.payment_required !== filters.payment) return false;
  if (filters.minLevel !== undefined && item.verification_detail.level < filters.minLevel) return false;
  if (filters.reviewed !== undefined && item.verification_detail.reviewed !== filters.reviewed) return false;
  if (filters.provider) {
    const organization = item.backing?.declared_provider?.organization || item.provider?.organization || '';
    if (!organization.toLowerCase().includes(filters.provider)) return false;
  }
  if (filters.hosting && item.backing?.hosting?.platform !== filters.hosting) return false;
  if (filters.protocol && !(Array.isArray(item.protocols) &&
      item.protocols.some(p => String(p).toUpperCase() === filters.protocol))) return false;
  if (filters.freshDays !== undefined) {
    const checked = full.checks.endpoint.checked_at ? Date.parse(full.checks.endpoint.checked_at) : NaN;
    if (!Number.isFinite(checked) || now.getTime() - checked > filters.freshDays * DAY) return false;
  }
  return true;
}

const KEYLESS_RANK = { declared: 0, unknown: 1, no: 2 };

/** Stable re-sort of an already relevance-ordered list. Explicit only; never the default. */
export function sortEntries(entries, sort) {
  const indexed = entries.map((entry, index) => ({ entry, index }));
  const key = {
    level: ({ entry }) => -entry.item.verification_detail.level,
    keyless_first: ({ entry }) => KEYLESS_RANK[entry.item.access?.keyless] ?? 1,
    recently_checked: ({ entry }) => {
      const at = entry.full.checks.endpoint.checked_at;
      return at ? -Date.parse(at) : 0;
    },
  }[sort];
  if (!key) return entries;
  return indexed.sort((a, b) => key(a) - key(b) || a.index - b.index).map(({ entry }) => entry);
}
