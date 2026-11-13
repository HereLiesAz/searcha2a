// Shared fixtures: a real in-memory SQLite engine behind the AgentRegistry Durable Object.
import { DatabaseSync } from 'node:sqlite';
import { AgentRegistry, handle } from '../../worker/index.mjs';

export function registryEnvironment({ extraEnv = {} } = {}) {
  const database = new DatabaseSync(':memory:');
  const storage = {
    sql: {
      exec(query, ...args) {
        const stmt = database.prepare(query);
        if (/^\s*SELECT\b/i.test(query)) {
          const rows = stmt.all(...args);
          return {
            toArray: () => rows,
            one: () => {
              if (rows.length !== 1) throw new Error('SQL cursor expected exactly one row');
              return rows[0];
            },
          };
        }
        stmt.run(...args);
        return { toArray: () => [], one: () => { throw new Error('No returned row'); } };
      },
    },
    transactionSync(fn) {
      database.exec('BEGIN');
      try {
        const value = fn();
        database.exec('COMMIT');
        return value;
      } catch (error) {
        database.exec('ROLLBACK');
        throw error;
      }
    },
  };
  const object = new AgentRegistry({ storage }, {});
  return {
    env: {
      ALLOWED_ORIGIN: 'https://searcha2a.azphalt.store',
      RESULT_CALLBACK_SECRET: 'pipeline-secret',
      REGISTRY_ADMIN_SECRET: 'admin-only-secret',
      REGISTRY: { idFromName: name => name, get: () => object },
      ...extraEnv,
    },
    sql: storage.sql,
    close: () => database.close(),
  };
}

export const card = (id, name, skill, description = 'Independent A2A agent', extra = {}) => ({
  id, name, description, endpoint: 'https://agents.example/a2a',
  source_url: `https://agents.example/cards/${id}.json`,
  skills: [{ name: skill, description: skill + ' service', tags: [skill.toLowerCase()] }],
  source: 'registry', verification: 'card_read_only', protocols: [],
  capabilities: {}, security_schemes: [], version: '1', ...extra,
});

export const request = (url, options = {}) => new Request('https://api.example' + url, {
  ...options, headers: { Origin: 'https://searcha2a.azphalt.store', ...options.headers },
});

export const ingest = (env, records, authorization = 'Bearer pipeline-secret') =>
  handle(request('/internal/registry/ingest', {
    method: 'POST', headers: { Authorization: authorization },
    body: JSON.stringify({ query: '', results: records, generated_at: '2026-11-12T12:00:00Z' }),
  }), env);

export const reportHealth = (env, results) =>
  handle(request('/internal/registry/health', {
    method: 'POST', headers: { Authorization: 'Bearer pipeline-secret' },
    body: JSON.stringify({ results }),
  }), env);

export const manage = (env, id, action, reason = 'test') =>
  handle(request('/internal/registry/manage', {
    method: 'POST', headers: { Authorization: 'Bearer admin-only-secret' },
    body: JSON.stringify({ id, action, reason }),
  }), env);

export const getJson = async (env, url) => {
  const response = await handle(request(url), env);
  return { status: response.status, body: await response.json(), headers: response.headers };
};

export { handle };
