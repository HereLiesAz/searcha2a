/**
 * ANS names supplied by publishers are claims, never proof of identity.
 * Version syntax belongs to the publisher; only DNS lexical rules apply.
 * Explicit agentHost disambiguates an arbitrary dotted version from the hostname.
 */
const LABEL = /^(?!-)[a-z0-9-]{1,63}(?<!-)$/i;

export function normalizeAnsName(raw) {
  if (typeof raw !== 'string' || raw.length > 259 || !raw.startsWith('ans://')) return null;
  const authority = raw.slice(6);
  const labels = authority.split('.');
  if (labels.length < 3 || authority.length > 253 ||
      labels.some(label => !LABEL.test(label))) return null;
  return raw.toLowerCase();
}

export function parseAnsClaim(rawName, rawHost) {
  const key = normalizeAnsName(rawName);
  if (!key || typeof rawHost !== 'string' || rawHost.length > 237) return null;
  const host = rawHost.toLowerCase();
  const labels = host.split('.');
  if (labels.length < 2 || labels.some(label => !LABEL.test(label)) ||
      host.endsWith('.local') || host.endsWith('.internal') || host.endsWith('.localhost')) return null;
  const suffix = '.' + host;
  if (!key.slice(6).endsWith(suffix)) return null;
  const version = rawName.slice(6, -suffix.length);
  if (!version || version.split('.').some(label => !LABEL.test(label))) return null;
  return { name: rawName, agent_host: rawHost, version, verification: 'unverified' };
}
