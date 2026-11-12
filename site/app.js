const form = document.querySelector('#search-form');
const queryInput = document.querySelector('#query');
const button = document.querySelector('#search-button');
const indexedButton = document.querySelector('#indexed-search-button');
const status = document.querySelector('#status');
const results = document.querySelector('#results');
const heading = document.querySelector('#result-heading');
const count = document.querySelector('#result-count');
let config = {};
let widgetId;
let viewRevision = 0; // Ignore responses from an earlier search or initial catalog load.

function textNode(tag, value, className = '') {
  const element = document.createElement(tag);
  element.textContent = String(value ?? '');
  if (className) element.className = className;
  return element;
}

function addLink(container, label, url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:') return;
    const link = textNode('a', label);
    link.href = parsed.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    container.append(link);
  } catch { /* Ignore malformed URLs from external agent cards. */ }
}

function display(records) {
  results.replaceChildren();
  const items = Array.isArray(records) ? records : [];
  count.textContent = `${items.length} agents`;
  if (!items.length) {
    results.append(textNode('div', 'No matching Agent Cards found. Discovery coverage depends on public registries and indexed GitHub cards.', 'empty'));
    return;
  }
  for (const agent of items) {
    const card = textNode('article', '', 'result');
    card.append(textNode('h3', agent.name), textNode('p', agent.description || 'No description supplied.'));
    const tags = textNode('div', '', 'tags');
    for (const skill of (agent.skills || []).slice(0, 6)) tags.append(textNode('span', skill.name, 'tag'));
    card.append(tags, textNode('small', `${agent.source || 'discovered'} · ${agent.verification || 'unverified'}`));
    const links = textNode('div', '', 'links');
    addLink(links, 'Agent Card ↗', agent.source_url);
    addLink(links, 'Endpoint ↗', agent.endpoint);
    card.append(links);
    results.append(card);
  }
}

async function jsonRequest(url, options) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function initialize() {
  try { config = await jsonRequest('./config.json'); } catch { config = {}; }
  try {
    const catalog = await jsonRequest('./api/v1/catalog.json');
    if (viewRevision === 0) display(catalog.results);
    if (viewRevision === 0) status.textContent = catalog.generated_at ? `Index refreshed ${new Date(catalog.generated_at).toLocaleString()}. Live searches start new Actions runs.` : 'Public index is not built yet.';
  } catch { if (viewRevision === 0) status.textContent = 'Public index unavailable.'; }
  if (!config.apiBaseUrl) {
    button.disabled = true;
    indexedButton.disabled = true;
    status.textContent += ' Search awaits gateway configuration.';
    return;
  }
  if (!config.turnstileSiteKey) {
    button.disabled = true;
    status.textContent += ' Live search awaits spam challenge configuration.';
    return;
  }
  const script = document.createElement('script');
  script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  script.async = true;
  script.onload = () => { widgetId = window.turnstile.render('#challenge', { sitekey: config.turnstileSiteKey }); };
  script.onerror = () => { button.disabled = true; status.textContent = 'Spam challenge could not load.'; };
  document.head.append(script);
}

indexedButton.addEventListener('click', async () => {
  if (indexedButton.disabled) return;
  const query = queryInput.value.trim();
  if (query.length < 2 || query.length > 120) {
    status.textContent = 'Enter 2–120 characters to search indexed agents.';
    return;
  }
  indexedButton.disabled = true;
  const revision = ++viewRevision;
  status.textContent = 'Matching indexed agents…';
  heading.textContent = 'Indexed matches for “' + query + '”';
  try {
    const base = config.apiBaseUrl.replace(/\/$/, '');
    const data = await jsonRequest(base + '/api/v1/directory?q=' + encodeURIComponent(query));
    if (revision !== viewRevision) return;
    display(data.results);
    status.textContent = data.match_mode === 'semantic'
      ? 'Ranked by text embeddings over the published catalog. Agent Cards remain unverified.'
      : 'Embedding search unavailable. Showing keyword-ranked catalog matches instead.';
  } catch (error) {
    if (revision === viewRevision) status.textContent = 'Indexed search failed: ' + error.message;
  } finally {
    indexedButton.disabled = false;
  }
});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (button.disabled) return;
  const query = queryInput.value.trim();
  const turnstile = window.turnstile?.getResponse(widgetId);
  if (!turnstile) { status.textContent = 'Complete the verification challenge to start a search.'; return; }
  button.disabled = true;
  const revision = ++viewRevision;
  status.textContent = 'Dispatching GitHub Actions search…';
  heading.textContent = `Results for “${query}”`;
  try {
    const base = config.apiBaseUrl.replace(/\/$/, '');
    const request = await jsonRequest(`${base}/api/v1/search`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, turnstile }),
    });
    if (revision !== viewRevision) return;
    status.textContent = 'Search queued in GitHub Actions. Waiting for results…';
    for (let attempt = 0; attempt < 48; attempt++) {
      await sleep(3000);
      if (revision !== viewRevision) return;
      const data = await jsonRequest(`${base}/api/v1/results/${encodeURIComponent(request.id)}`);
      if (revision !== viewRevision) return;
      if (data.status === 'complete') {
        display(data.results);
        status.textContent = `GitHub Actions search complete. ${data.count} cards matched.${data.warnings?.length ? ' Some sources were unavailable.' : ''}`;
        return;
      }
    }
    if (revision === viewRevision) status.textContent = 'Search is still running. Check the workflow or retry shortly.';
  } catch (error) {
    if (revision === viewRevision) status.textContent = `Search failed: ${error.message}`;
  } finally {
    button.disabled = false;
    if (widgetId !== undefined) window.turnstile?.reset(widgetId);
  }
});
initialize();
