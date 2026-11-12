const form = document.querySelector('#search-form');
const queryInput = document.querySelector('#query');
const button = document.querySelector('#search-button');
const status = document.querySelector('#status');
const results = document.querySelector('#results');
const heading = document.querySelector('#result-heading');
const count = document.querySelector('#result-count');
let config = {};
let apiBaseUrl = '';

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

async function searchRegistry(query) {
  const path = `/api/v1/agents?query=${encodeURIComponent(query)}&limit=70`;
  const data = await jsonRequest(apiBaseUrl + path);
  display(data.results);
  status.textContent = `${data.count} matches in ${data.total} registered agents. ${data.generated_at ? 'Registry updated ' + new Date(data.generated_at).toLocaleString() + '.' : 'Registry has not been populated yet.'}`;
}

async function initialize() {
  button.disabled = true;
  try {
    const config = await jsonRequest('./config.json');
    if (!config.apiBaseUrl || !config.apiBaseUrl.startsWith('https://')) {
      status.textContent = 'Registry API configuration unavailable.';
      return;
    }
    apiBaseUrl = config.apiBaseUrl.replace(/\/$/, '');
    await searchRegistry('');
    status.textContent += ' Searches read the registry directly; crawls run independently.';
    button.disabled = false;
  } catch (error) {
    status.textContent = `Registry unavailable: ${error.message}`;
  }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (button.disabled) return;
  const query = queryInput.value.trim();
  if (query.length < 2 || query.length > 120) return;
  button.disabled = true;
  heading.textContent = `Results for “${query}”`;
  status.textContent = 'Searching registry…';
  try {
    await searchRegistry(query);
  } catch (error) {
    display([]);
    status.textContent = `Registry search failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});
initialize();
