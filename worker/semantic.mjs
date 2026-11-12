/**
 * Read-only search of SearchA2A's published, metadata-only Agent Card feed.
 * Semantic ranking uses Workers AI embeddings; when unavailable the response says
 * "keyword-fallback" instead of pretending lexical matches are semantic.
 * No Agent Card, task endpoint, credential, or GitHub Actions dispatch is touched.
 */
const CATALOG_URL = 'https://raw.githubusercontent.com/HereLiesAz/searcha2a/agent-feed/site/api/v1/azphalt-listings.json';
const EMBEDDING_MODEL = '@cf/baai/bge-small-en-v1.5';
const MAX_CATALOG_BYTES = 16_777_216;
const MAX_RESULTS = 20;
const STOP_WORDS = new Set(['a', 'an', 'and', 'at', 'by', 'can', 'find', 'for', 'from', 'in', 'me', 'of', 'on', 'or', 'the', 'to', 'who', 'with']);

function words(text) {
  return (String(text).toLowerCase().match(/[a-z0-9]+/g) || []).filter(word => word.length > 1 && !STOP_WORDS.has(word));
}

function descriptionOf(agent) {
  const skillText = (Array.isArray(agent.skills) ? agent.skills : []).slice(0, 8).flatMap(skill => [
    skill?.name || '', skill?.description || '',
    ...(Array.isArray(skill?.tags) ? skill.tags.slice(0, 5) : []),
  ]).join(' ');
  return [agent.name || '', skillText, agent.description || ''].join(' ').slice(0, 900);
}

/** Fixed keyword fallback, normalized by the query terms, not a substitute for embeddings. */
export function keywordScore(query, agent) {
  const terms = [...new Set(words(query))];
  if (!terms.length) return 0;
  const name = new Set(words(agent.name || ''));
  const skills = new Set(words((Array.isArray(agent.skills) ? agent.skills : [])
    .flatMap(s => [s?.name || '', s?.description || '', ...(Array.isArray(s?.tags) ? s.tags : [])])
    .join(' ')));
  const about = new Set(words(agent.description || ''));
  let score = 0;
  for (const term of terms) score += name.has(term) ? 5 : skills.has(term) ? 4 : about.has(term) ? 1 : 0;
  return Math.min(1, score / (terms.length * 5));
}

export function cosineSimilarity(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || !a.length || a.length !== b.length) return null;
  let dot = 0, aa = 0, bb = 0;
  for (let i = 0; i < a.length; i++) {
    if (!Number.isFinite(a[i]) || !Number.isFinite(b[i])) return null;
    dot += a[i] * b[i];
    aa += a[i] * a[i];
    bb += b[i] * b[i];
  }
  if (!aa || !bb) return null;
  return dot / Math.sqrt(aa * bb);
}

function publicCardUrl(raw) {
  if (typeof raw !== 'string' || raw.length > 2048) return false;
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    return url.protocol === 'https:' && !url.username && !url.password &&
      !url.hash && !url.search && host.includes('.') &&
      !host.endsWith('.localhost') && !host.endsWith('.local') && !host.endsWith('.internal') &&
      host !== 'localhost' && !/^\d+\.\d+\.\d+\.\d+$/.test(host) && !host.includes(':');
  } catch { return false; }
}

/** Stream-capped JSON; checking Content-Length alone cannot bound chunked responses. */
async function readBoundedText(response, limit) {
  if (!response.body) throw new Error('Catalog response body missing');
  const reader = response.body.getReader();
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > limit) {
        await reader.cancel();
        throw new Error('Catalog too large');
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

/** Read the mirrored feed rather than introducing another independently maintained registry. */
export async function readPublishedAgents(fetcher = fetch) {
  const response = await fetcher(CATALOG_URL, {
    redirect: 'manual',
    signal: AbortSignal.timeout(8_000),
    cf: { cacheEverything: true, cacheTtl: 300 },
  });
  if (!response.ok || (Number(response.headers.get('content-length') || 0) > MAX_CATALOG_BYTES)) {
    throw new Error('Catalog not available');
  }
  const text = await readBoundedText(response, MAX_CATALOG_BYTES);
  const feed = JSON.parse(text);
  if (feed?.schema !== 1 || feed.source !== 'searcha2a' || !Array.isArray(feed.listings)) {
    throw new Error('Unexpected catalog schema');
  }
  const entries = feed.listings.filter(agent => agent && agent.kind === 'a2a-agent' &&
    agent.installable === false && agent.verification === 'card_read_only' &&
    typeof agent.name === 'string' && publicCardUrl(agent.source_url));
  return { generated_at: typeof feed.generated_at === 'string' ? feed.generated_at : null,
    entries };
}

function validEmbeddings(data, count) {
  if (!Array.isArray(data) || data.length !== count || !Array.isArray(data[0])) return false;
  const dimensions = data[0].length;
  return dimensions >= 2 && data.every(vector => Array.isArray(vector) &&
    vector.length === dimensions && vector.every(Number.isFinite));
}

/**
 * A read-only capability lookup. Registry claims are never promoted to verified
 * identities, connection approvals, or permissions.
 */
export async function rankAgentCandidates(query, env, entries, generated_at = null) {
  let embeddings = null;
  if (entries.length && env.AI && typeof env.AI.run === 'function') {
    try {
      // bge-small-en-v1.5 accepts at most 100 inputs per call. Include the same
      // query in each bounded batch, then reunite the candidate vectors.
      const queryText = 'Represent this sentence for searching relevant passages: ' + query;
      const vectors = [];
      for (let start = 0; start < entries.length; start += 99) {
        const descriptions = entries.slice(start, start + 99).map(descriptionOf);
        const response = await env.AI.run(EMBEDDING_MODEL, {
          text: [queryText, ...descriptions],
          pooling: 'cls',
        });
        if (!validEmbeddings(response?.data, descriptions.length + 1) ||
            (vectors.length && response.data[0].length !== vectors[0].length)) {
          throw new Error('Invalid embedding batch');
        }
        if (!vectors.length) vectors.push(response.data[0]);
        vectors.push(...response.data.slice(1));
      }
      if (validEmbeddings(vectors, entries.length + 1)) embeddings = vectors;
    } catch {
      // A failed batch must not leave partially semantic results masquerading as valid.
      embeddings = null;
    }
  }
  const semantic = embeddings !== null;
  const ranked = entries.map((agent, i) => {
    const lexical = keywordScore(query, agent);
    const similarity = semantic ? cosineSimilarity(embeddings[0], embeddings[i + 1]) : null;
    const score = similarity === null ? lexical : 0.85 * Math.max(0, similarity) + 0.15 * lexical;
    return { agent, score };
  }).filter(candidate => candidate.score > 0)
    .sort((a, b) => b.score - a.score || a.agent.name.localeCompare(b.agent.name))
    .slice(0, MAX_RESULTS);
  return {
    schema: 1, source: 'searcha2a', generated_at, query,
    match_mode: semantic ? 'semantic' : 'keyword-fallback',
    results: ranked.map(({ agent, score }) => ({
      ...agent,
      match_score: Math.round(score * 10000) / 10000,
    })),
    count: ranked.length,
    verification: 'card_read_only',
  };
}

/** Compatibility path for standalone feed readers without a registry binding. */
export async function searchIndexedAgents(query, env, fetcher = fetch) {
  const { generated_at, entries } = await readPublishedAgents(fetcher);
  return rankAgentCandidates(query, env, entries, generated_at);
}
