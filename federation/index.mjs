/** Independently deployed, synchronous A2A v1 JSON-RPC -> HTTPS webhook adapter.
 * Does not import or invoke the SearchA2A registry, and never runs inference.
 */

const JSON_HEADERS = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' };
const REQUEST_LIMIT = 16_384;
const RESPONSE_LIMIT = 65_536;

function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), { status, headers: { ...JSON_HEADERS, ...headers } });
}

function isPublicHttps(value, originOnly = false) {
  if (typeof value !== 'string' || value.length > 2048) return false;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    if (url.protocol !== 'https:' || !host.includes('.') || url.username || url.password ||
        url.hash || url.port && url.port !== '443' || /(^|\.)(localhost|local|internal|test|invalid)$/.test(host) ||
        /^\d+(\.\d+){3}$/.test(host) || host.includes(':')) return false;
    return !originOnly || (url.pathname === '/' && !url.search && value.replace(/\/$/, '') === url.origin);
  } catch { return false; }
}

function validConfig(env) {
  if (!env?.GATEWAY_API_KEY || env.GATEWAY_API_KEY.length < 24 ||
      !isPublicHttps(env.UPSTREAM_URL) || !isPublicHttps(env.PUBLIC_ORIGIN, true) ||
      typeof env.AGENT_NAME !== 'string' || !env.AGENT_NAME.trim() ||
      typeof env.AGENT_DESCRIPTION !== 'string' || !env.AGENT_DESCRIPTION.trim() ||
      typeof env.AGENT_VERSION !== 'string' || !env.AGENT_VERSION.trim()) return false;
  let skills;
  try { skills = JSON.parse(env.AGENT_SKILLS_JSON); } catch { return false; }
  return Array.isArray(skills) && skills.length > 0 && skills.length <= 40 &&
    skills.every(skill => skill && typeof skill.id === 'string' && skill.id.trim() &&
      typeof skill.name === 'string' && skill.name.trim() &&
      typeof skill.description === 'string' && skill.description.trim() &&
      Array.isArray(skill.tags) && skill.tags.every(tag => typeof tag === 'string'));
}

function agentCard(env) {
  return {
    name: env.AGENT_NAME,
    description: env.AGENT_DESCRIPTION,
    version: env.AGENT_VERSION,
    supportedInterfaces: [{
      url: `${env.PUBLIC_ORIGIN.replace(/\/$/, '')}/a2a`,
      protocolBinding: 'JSONRPC',
      protocolVersion: '1.0',
    }],
    capabilities: { streaming: false, pushNotifications: false },
    defaultInputModes: ['text/plain'],
    defaultOutputModes: ['text/plain'],
    skills: JSON.parse(env.AGENT_SKILLS_JSON),
    securitySchemes: { gatewayBearer: { httpAuthSecurityScheme: { scheme: 'Bearer' } } },
    securityRequirements: [{ gatewayBearer: [] }],
  };
}

/** Cap actual bytes, not Content-Length (which callers can omit or falsify). */
async function boundedText(response, maxBytes) {
  if (Number(response.headers.get('content-length') || 0) > maxBytes) throw new Error('too large');
  if (!response.body) return '';
  const reader = response.body.getReader();
  const chunks = [];
  let count = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      count += value.byteLength;
      if (count > maxBytes) {
        await reader.cancel();
        throw new Error('too large');
      }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const out = new Uint8Array(count);
  let offset = 0;
  for (const chunk of chunks) { out.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder().decode(out);
}

function rpcError(id, code, message, status = 200) {
  return json({ jsonrpc: '2.0', id, error: { code, message } }, status);
}

/** Only text-only, synchronous webhook calls are supported in this adapter. */
async function handle(request, env, fetcher = fetch) {
  const url = new URL(request.url);
  if (!validConfig(env)) return json({ error: 'Federation gateway not configured' }, 503);
  if (request.method === 'GET' && url.pathname === '/.well-known/agent-card.json') {
    return json(agentCard(env));
  }
  if (url.pathname !== '/a2a') return json({ error: 'Not found' }, 404);
  if (request.method !== 'POST') return json({ error: 'Method not allowed' }, 405, { allow: 'POST' });
  if (request.headers.get('authorization') !== `Bearer ${env.GATEWAY_API_KEY}`) {
    return json({ error: 'Unauthorized' }, 401, { 'www-authenticate': 'Bearer' });
  }
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers.get('content-type') || '')) {
    return json({ error: 'Expected JSON' }, 415);
  }
  let input;
  try { input = JSON.parse(await boundedText(request, REQUEST_LIMIT)); }
  catch { return rpcError(null, -32700, 'Invalid or oversized JSON', 400); }
  const id = input && (typeof input.id === 'string' && input.id.length <= 128 ||
    typeof input.id === 'number' && Number.isFinite(input.id)) ? input.id : null;
  if (!input || input.jsonrpc !== '2.0' || id === null || typeof input.method !== 'string') {
    return rpcError(id, -32600, 'Invalid request', 400);
  }
  if (input.method !== 'SendMessage') return rpcError(id, -32601, 'Method not supported');
  const message = input.params?.message;
  const parts = message?.parts;
  if (!message || message.role !== 'ROLE_USER' || typeof message.messageId !== 'string' ||
      !message.messageId.trim() || message.messageId.length > 128 ||
      message.taskId !== undefined || message.contextId !== undefined &&
      (typeof message.contextId !== 'string' || !message.contextId || message.contextId.length > 128) ||
      !Array.isArray(parts) || !parts.length || parts.length > 16 ||
      parts.some(p => !p || typeof p.text !== 'string' ||
        ['raw', 'url', 'data'].some(k => Object.hasOwn(p, k)))) {
    return rpcError(id, -32602, 'Expected a text-only user message with messageId');
  }
  const text = parts.map(part => part.text).join('\n').trim();
  if (!text || text.length > 12_000) return rpcError(id, -32602, 'Message too long or empty');
  const contextId = message.contextId || crypto.randomUUID();
  let response;
  try {
    const headers = { 'content-type': 'application/json' };
    if (env.UPSTREAM_BEARER_TOKEN) headers.authorization = `Bearer ${env.UPSTREAM_BEARER_TOKEN}`;
    response = await fetcher(env.UPSTREAM_URL, {
      method: 'POST', headers, redirect: 'manual', signal: AbortSignal.timeout(15_000),
      body: JSON.stringify({ message: text, contextId, messageId: message.messageId }),
    });
    if (!response.ok) return rpcError(id, -32000, 'Owner agent unavailable', 502);
    const body = await boundedText(response, RESPONSE_LIMIT);
    const type = response.headers.get('content-type') || '';
    const parsed = /application\/json/i.test(type) ? JSON.parse(body) : body;
    const reply = typeof parsed === 'string' ? parsed :
      typeof parsed?.text === 'string' ? parsed.text :
      typeof parsed?.response === 'string' ? parsed.response : null;
    if (typeof reply !== 'string' || !reply.trim() || reply.length > 60_000) {
      return rpcError(id, -32000, 'Owner agent returned an unsupported response', 502);
    }
    return json({ jsonrpc: '2.0', id, result: { message: {
      messageId: crypto.randomUUID(), contextId, role: 'ROLE_AGENT', parts: [{ text: reply }],
    } } });
  } catch {
    return rpcError(id, -32000, 'Owner agent unavailable', 502);
  }
}

export { handle };
export default { fetch: (request, env) => handle(request, env) };
