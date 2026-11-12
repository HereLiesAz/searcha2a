import { indexedMatches, mergeResults } from './search-results.mjs';

const form = document.querySelector('#search-form');
const queryInput = document.querySelector('#query');
const button = document.querySelector('#search-button');
const status = document.querySelector('#status');
const results = document.querySelector('#results');
const heading = document.querySelector('#result-heading');
const count = document.querySelector('#result-count');
let config = {};
let widgetId;
let indexedCatalog = [];

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

function display(records, emptyMessage = 'No matching Agent Cards found. Discovery coverage depends on public registries and indexed GitHub cards.') {
  results.replaceChildren();
  const items = Array.isArray(records) ? records : [];
  count.textContent = `${items.length} agents`;
  if (!items.length) {
    results.append(textNode('div', emptyMessage, 'empty'));
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
    indexedCatalog = Array.isArray(catalog.results) ? catalog.results : [];
    display(indexedCatalog);
    status.textContent = catalog.generated_at ? `Index refreshed ${new Date(catalog.generated_at).toLocaleString()}. Live searches start new Actions runs.` : 'Public index is not built yet.';
  } catch { status.textContent = 'Public index unavailable.'; }
  if (!config.apiBaseUrl || !config.turnstileSiteKey) {
    button.disabled = true;
    status.textContent += ' Live search awaits gateway configuration.';
    return;
  }
  const script = document.createElement('script');
  script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  script.async = true;
  script.onload = () => { widgetId = window.turnstile.render('#challenge', { sitekey: config.turnstileSiteKey }); };
  script.onerror = () => { button.disabled = true; status.textContent = 'Spam challenge could not load.'; };
  document.head.append(script);
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (button.disabled) return;
  const query = queryInput.value.trim();
  if (query.length < 2 || query.length > 120) return;
  const immediate = indexedMatches(indexedCatalog, query);
  heading.textContent = `Results for “${query}”`;
  display(immediate, 'No indexed matches yet. A live search may find more.');
  const indexedCount = `${immediate.length} indexed ${immediate.length === 1 ? 'agent' : 'agents'}`;
  const turnstile = window.turnstile?.getResponse(widgetId);
  if (!turnstile) {
    status.textContent = `Showing ${indexedCount}. Complete verification to search GitHub for more.`;
    return;
  }
  button.disabled = true;
  status.textContent = `Showing ${indexedCount}. More results are coming when GitHub Actions finishes. Dispatching search…`;
  try {
    const base = config.apiBaseUrl.replace(/\/$/, '');
    const request = await jsonRequest(`${base}/api/v1/search`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, turnstile }),
    });
    status.textContent = `Showing ${indexedCount}. More results are coming when GitHub Actions finishes.`;
    for (let attempt = 0; attempt < 48; attempt++) {
      await sleep(3000);
      const data = await jsonRequest(`${base}/api/v1/results/${encodeURIComponent(request.id)}`);
      if (data.status === 'complete') {
        const combined = mergeResults(immediate, data.results);
        display(combined);
        status.textContent = `Search complete. ${combined.length} agents found (indexed and live).${data.warnings?.length ? ' Some sources were unavailable.' : ''}`;
        return;
      }
    }
    status.textContent = `Showing ${indexedCount}. Live search is still running; additional results have not arrived.`;
  } catch (error) {
    status.textContent = `Showing ${indexedCount}. Live search failed: ${error.message}`;
  } finally {
    button.disabled = false;
    if (widgetId !== undefined) window.turnstile?.reset(widgetId);
  }
});
initialize();
