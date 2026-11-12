/** Match already-published Agent Card metadata while the live Actions search runs. */
export function indexedMatches(records, query) {
  const terms = String(query ?? '').toLocaleLowerCase().match(/[\p{L}\p{N}_-]+/gu) || [];
  if (!Array.isArray(records) || terms.length === 0) return [];
  return records.map((agent, index) => {
    if (!agent || typeof agent !== 'object') return null;
    const name = String(agent.name ?? '').toLocaleLowerCase();
    const description = String(agent.description ?? '').toLocaleLowerCase();
    const skills = Array.isArray(agent.skills) ? agent.skills : [];
    const skillText = skills.map(skill => {
      if (!skill || typeof skill !== 'object') return '';
      const tags = Array.isArray(skill.tags) ? skill.tags : [];
      return [skill.name, skill.description, ...tags].filter(x => typeof x === 'string').join(' ');
    }).join(' ').toLocaleLowerCase();
    const score = terms.reduce((sum, term) => sum +
      (name.includes(term) ? 8 : 0) +
      (skillText.includes(term) ? 4 : 0) +
      (description.includes(term) ? 1 : 0), 0);
    return score > 0 ? { agent, score, index } : null;
  }).filter(Boolean).sort((a, b) => b.score - a.score || a.index - b.index)
    .map(item => item.agent);
}

/** Keep indexed cards visible and refresh/append them with completed live results. */
export function mergeResults(indexed, live) {
  const records = new Map();
  let anonymous = 0;
  for (const agent of [...(Array.isArray(indexed) ? indexed : []), ...(Array.isArray(live) ? live : [])]) {
    if (!agent || typeof agent !== 'object') continue;
    const id = typeof agent.id === 'string' && agent.id ? agent.id : null;
    const source = typeof agent.source_url === 'string' && agent.source_url ? agent.source_url : null;
    const key = id ? `id:${id}` : source ? `url:${source}` : `unknown:${anonymous++}`;
    records.set(key, agent);
  }
  return [...records.values()];
}
