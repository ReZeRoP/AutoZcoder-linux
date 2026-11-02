/* Execute the actual bundled template strings; fetch is always an offline stub. */
'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON || 'python3';
const templates = JSON.parse(execFileSync(python, ['-c', `
import ast,json,pathlib
p=ast.parse(pathlib.Path('zcode_gateway/relay_deploy.py').read_text())
values={}
for node in p.body:
    if isinstance(node,ast.Assign):
        names=[n.id for n in node.targets if isinstance(n,ast.Name)]
        if any(n in ('RELAY_GUARD_JS','_RELAY_HANDLER','CLOUDFLARE_WORKER_CODE','VERCEL_RELAY_CODE','DENO_RELAY_CODE') for n in names):
            exec(compile(ast.Module(body=[node],type_ignores=[]),'<template>','exec'),{},values)
print(json.dumps({k: v.replace('__RELAY_SECRET__', '').replace('__RELAY_ALLOW__', 'provider.example')
                 for k,v in values.items() if k not in ('_RELAY_HANDLER', 'RELAY_GUARD_JS')}))
`], {cwd: root, encoding: 'utf8'}));

function loadTemplate(source, fetch) {
  let handler;
  const context = {Headers, Request, Response, URL, fetch,
    Deno: {serve(fn) { handler = fn; }},
    setDefault(value) { handler = typeof value === 'function' ? value : value.fetch; }};
  source = source.replace('export const config', 'const config')
    .replace('export default { fetch: relay };', 'setDefault({ fetch: relay });')
    .replace('export default relay;', 'setDefault(relay);');
  vm.runInNewContext(source, context, {timeout: 1000});
  assert.equal(typeof handler, 'function');
  return handler;
}

for (const [name, source] of Object.entries(templates)) {
  test(`${name} streams before body completion and overwrites spoofed markers`, async () => {
    let streamController, cancelled = false, forwarded;
    const stream = new ReadableStream({
      start(controller) { streamController = controller; controller.enqueue(new TextEncoder().encode('first')); },
      cancel() { cancelled = true; },
    });
    const handler = loadTemplate(source, async (url, init) => {
      forwarded = {url, init};
      return new Response(stream, {status: 429, headers: {
        'content-type': 'text/event-stream',
        'x-autozcoder-response-origin': 'relay',
        'x-autozcoder-relay-version': 'spoofed',
        'retry-after': '12',
      }});
    });
    const req = new Request('https://relay.example/', {headers: {
      'x-relay-target': 'https://provider.example', 'x-relay-path': '/messages?x=1',
      'x-autozcoder-response-origin': 'relay', 'x-autozcoder-relay-version': 'spoofed',
      'authorization': 'Bearer synthetic-only',
    }});
    const res = await handler(req);
    assert.equal(res.status, 429);
    assert.equal(res.headers.get('x-autozcoder-response-origin'), 'upstream');
    assert.equal(res.headers.get('x-autozcoder-relay-version'), '1');
    assert.equal(res.headers.get('retry-after'), '12');
    assert.equal(forwarded.url, 'https://provider.example/messages?x=1');
    for (const key of ['x-relay-target', 'x-relay-path', 'host',
                       'x-autozcoder-response-origin', 'x-autozcoder-relay-version']) {
      assert.equal(forwarded.init.headers.has(key), false);
    }
    assert.equal(forwarded.init.headers.get('authorization'), 'Bearer synthetic-only');
    const reader = res.body.getReader();
    const first = await reader.read();
    assert.equal(new TextDecoder().decode(first.value), 'first');
    // The upstream has not closed: proving the handler did not buffer its body.
    streamController.enqueue(new TextEncoder().encode('second'));
    assert.equal(new TextDecoder().decode((await reader.read()).value), 'second');
    await reader.cancel();
    assert.equal(cancelled, true);
  });

  test(`${name} marks locally generated errors and hides fetch exception`, async () => {
    let calls = 0;
    const handler = loadTemplate(source, async () => { calls++; throw Error('secret-provider-token'); });
    const missing = await handler(new Request('https://relay.example/'));
    assert.equal(missing.status, 400);
    assert.equal(missing.headers.get('x-autozcoder-response-origin'), 'relay');
    assert.equal(missing.headers.get('x-autozcoder-relay-version'), '1');
    assert.equal(calls, 0);
    const failed = await handler(new Request('https://relay.example/', {headers: {'x-relay-target': 'https://provider.example'}}));
    assert.equal(failed.status, 502);
    assert.equal(failed.headers.get('x-autozcoder-response-origin'), 'relay');
    assert.equal(failed.headers.get('x-autozcoder-relay-version'), '1');
    assert.equal((await failed.text()).includes('secret'), false);
    assert.equal(calls, 1);
  });
}
