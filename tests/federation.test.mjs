import assert from 'node:assert/strict';
import { test } from 'node:test';
import { handle } from '../federation/index.mjs';

const env = {
  PUBLIC_ORIGIN: 'https://owner.workers.dev',
  UPSTREAM_URL: 'https://agent.example.com/invoke',
  GATEWAY_API_KEY: '0123456789abcdefghijklmnopqrstuvwxyz',
  UPSTREAM_BEARER_TOKEN: 'upstream-secret',
  AGENT_NAME: 'Test Agent',
  AGENT_DESCRIPTION: 'An owner-operated chat adapter.',
  AGENT_VERSION: '2026.11',
  AGENT_SKILLS_JSON: JSON.stringify([{
    id: 'text-chat', name: 'Text conversation', description: 'Reply to text', tags: ['text'],
  }]),
};
const validRpc = {
  jsonrpc: '2.0', id: 9, method: 'SendMessage',
  params: { message: { role: 'ROLE_USER', messageId: 'msg-1',
    contextId: 'thread-1', parts: [{ text: 'Hello' }] } },
};
const post = (body = validRpc, overrides = {}) => new Request('https://owner.workers.dev/a2a', {
  method: 'POST', headers: {
    authorization: `Bearer ${env.GATEWAY_API_KEY}`, 'content-type': 'application/json',
    ...overrides,
  }, body: typeof body === 'string' ? body : JSON.stringify(body),
});

test('disabled by default when owner credentials are missing', async () => {
  const response = await handle(new Request('https://owner.workers.dev/.well-known/agent-card.json'), {});
  assert.equal(response.status, 503);
});

test('published card advertises only implemented A2A v1 JSON-RPC and bearer security', async () => {
  const response = await handle(new Request('https://owner.workers.dev/.well-known/agent-card.json'), env);
  assert.equal(response.status, 200);
  const card = await response.json();
  assert.deepEqual(card.supportedInterfaces, [{
    url: 'https://owner.workers.dev/a2a', protocolBinding: 'JSONRPC', protocolVersion: '1.0',
  }]);
  assert.deepEqual(card.capabilities, { streaming: false, pushNotifications: false });
  assert.deepEqual(card.securitySchemes, {
    gatewayBearer: { httpAuthSecurityScheme: { scheme: 'Bearer' } },
  });
  assert.deepEqual(card.securityRequirements, [{ gatewayBearer: [] }]);
  assert.deepEqual(card.defaultInputModes, ['text/plain']);
  assert.deepEqual(card.defaultOutputModes, ['text/plain']);
  assert.equal(card.name, 'Test Agent');
  assert.equal(card.version, '2026.11');
  assert.equal(card.skills[0].id, 'text-chat');
  assert.ok(!JSON.stringify(card).includes('upstream-secret'));
  assert.ok(!JSON.stringify(card).includes('agent.example.com'));
});

test('no unauthenticated execution; never sends caller bearer token upstream', async () => {
  let called = false;
  const response = await handle(post(validRpc, { authorization: 'Bearer wrong' }), env, async () => {
    called = true;
    throw new Error('should not run');
  });
  assert.equal(response.status, 401);
  assert.equal(response.headers.get('www-authenticate'), 'Bearer');
  assert.equal(called, false);
});

test('authenticated SendMessage invokes only the owner-configured HTTPS endpoint', async () => {
  let calls = 0;
  const fetcher = async (url, init) => {
    calls++;
    assert.equal(url, 'https://agent.example.com/invoke');
    assert.equal(init.method, 'POST');
    assert.equal(init.redirect, 'manual');
    assert.equal(init.headers.authorization, 'Bearer upstream-secret');
    assert.ok(!JSON.stringify(init).includes(env.GATEWAY_API_KEY));
    assert.deepEqual(JSON.parse(init.body), {
      message: 'Hello', contextId: 'thread-1', messageId: 'msg-1',
    });
    return new Response(JSON.stringify({ text: 'Greetings, human.' }), {
      headers: { 'content-type': 'application/json' },
    });
  };
  const response = await handle(post(), env, fetcher);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.jsonrpc, '2.0');
  assert.equal(body.id, 9);
  assert.equal(body.result.message.role, 'ROLE_AGENT');
  assert.equal(body.result.message.contextId, 'thread-1');
  assert.equal(typeof body.result.message.messageId, 'string');
  assert.deepEqual(body.result.message.parts, [{ text: 'Greetings, human.' }]);
  assert.equal(calls, 1);
});

test('unsupported methods and unsafe parts do not call upstream', async () => {
  const fetcher = () => { throw new Error('unexpected'); };
  const method = await handle(post({ ...validRpc, method: 'SendStreamingMessage' }), env, fetcher);
  assert.equal((await method.json()).error.code, -32601);
  const binary = { ...validRpc, params: { message: { ...validRpc.params.message,
    parts: [{ text: 'Hi', url: 'https://evil.example/file' }] } } };
  const invalid = await handle(post(binary), env, fetcher);
  assert.equal((await invalid.json()).error.code, -32602);
  const task = { ...validRpc, params: { message: { ...validRpc.params.message, taskId: 'task-2' } } };
  const noTasks = await handle(post(task), env, fetcher);
  assert.equal((await noTasks.json()).error.code, -32602);
});

test('rejects invalid JSON and large bodies even when chunked', async () => {
  assert.equal((await handle(post('{not-json'), env)).status, 400);
  const huge = post(JSON.stringify({ ...validRpc, padding: 'x'.repeat(20_000) }));
  const response = await handle(huge, env);
  assert.equal(response.status, 400);
  assert.equal((await response.json()).error.code, -32700);
});

test('upstream redirects and unrecognized responses fail closed', async () => {
  const redirect = await handle(post(), env,
    async () => new Response(null, { status: 302, headers: { location: 'http://localhost/private' } }));
  assert.equal(redirect.status, 502);
  const malformed = await handle(post(), env,
    async () => new Response(JSON.stringify({ unexpected: true }), {
      headers: { 'content-type': 'application/json' },
    }));
  assert.equal(malformed.status, 502);
});

test('private addresses, missing keys, and client-supplied URLs are never executable', async () => {
  for (const upstream of ['http://agent.example.com', 'https://127.0.0.1/chat',
    'https://metadata.google.internal/latest', 'https://[::1]/', 'https://localhost/']) {
    const response = await handle(post(), { ...env, UPSTREAM_URL: upstream }, () => {
      throw new Error('unexpected');
    });
    assert.equal(response.status, 503, upstream);
  }
  const response = await handle(post({ ...validRpc, upstream: 'https://evil.example/steal' }),
    env, async (url) => {
      assert.equal(url, 'https://agent.example.com/invoke');
      return new Response('OK');
    });
  assert.equal(response.status, 200);
});

test('plain-text upstream and absent context are supported', async () => {
  const request = post({ ...validRpc, params: { message: {
    role: 'ROLE_USER', messageId: 'msg-2', parts: [{ text: 'Ping' }],
  } } });
  const response = await handle(request, { ...env, UPSTREAM_BEARER_TOKEN: '' }, async (_url, init) => {
    assert.equal(init.headers.authorization, undefined);
    return new Response('Pong', { headers: { 'content-type': 'text/plain' } });
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.result.message.parts[0].text, 'Pong');
  assert.ok(body.result.message.contextId.length > 10);
});

test('operator metadata is required and never inferred from repository defaults', async () => {
  for (const bad of [{ AGENT_NAME: '' }, { AGENT_SKILLS_JSON: '[broken' },
      { AGENT_SKILLS_JSON: '[]' }, { AGENT_SKILLS_JSON: '[{"name":"missing-id"}]' }]) {
    const response = await handle(new Request('https://owner.workers.dev/.well-known/agent-card.json'),
      { ...env, ...bad });
    assert.equal(response.status, 503);
  }
});
