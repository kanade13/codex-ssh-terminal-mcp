const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/native-panel-client.js'), 'utf8');
const testInput = 'PRIVATE_TEST_INPUT\r';
const toolError = data => ({ isError: true, content: [{ type: 'text', text: JSON.stringify(data) }] });

function panel(publicKey, { legacy = false, loseReply = false } = {}) {
  let receive, worker = 'initial', bootstraps = 0, delivered = 0;
  const requests = [];
  const parent = {
    postMessage(request) {
      if (request.id == null) return;
      queueMicrotask(() => {
        const { id, method, params } = request;
        let result, error;
        if (method === 'ui/initialize') {
          result = {};
        } else if (params.name === 'panel_bootstrap') {
          bootstraps++;
          result = { _meta: { sshBridge: {
            transport: 'mcp', encryption: { publicKey, keyId: worker }, privateClipboard: true,
          } } };
        } else if (params.name === 'panel_private_input') {
          const { envelope } = params.arguments;
          requests.push(envelope);
          if (envelope.keyId !== worker) {
            result = toolError(legacy
              ? { error: 'Private input is invalid, expired, or already used. Enter it again.' }
              : { code: 'PANEL_KEY_EXPIRED' });
          } else {
            delivered++;
            if (loseReply) error = { message: 'Response lost after dispatch' };
            else result = { _meta: { payload: { ok: true } } };
          }
        } else {
          result = { _meta: { payload: { ok: true } } };
        }
        receive({ source: parent, data: { jsonrpc: '2.0', id, result, error } });
      });
    },
  };
  const window = { parent, addEventListener: (name, handler) => { receive = handler; } };
  const document = {
    createElement: () => ({ addEventListener() {}, appendChild() {} }),
    querySelector: () => ({ after() {} }),
    head: { appendChild() {} },
  };
  vm.runInNewContext(source, { window, document, crypto: webcrypto, atob, btoa, TextEncoder,
    TextDecoder, Response, setTimeout, clearTimeout });
  return {
    prepare: () => window.codexSSHNative.fetch('/api/status'),
    send: () => window.codexSSHNative.fetch('/api/input', {
      method: 'POST', body: JSON.stringify({ sessionId: 'local-test', data: testInput, terminal: true }),
    }),
    restart: () => { worker = 'replacement'; },
    counts: () => ({ bootstraps, delivered, requests: requests.length }),
    requests,
  };
}

(async () => {
  const pair = await webcrypto.subtle.generateKey({
    name: 'RSA-OAEP', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256',
  }, true, ['encrypt', 'decrypt']);
  const publicKey = Buffer.from(await webcrypto.subtle.exportKey('spki', pair.publicKey)).toString('base64');

  const current = panel(publicKey);
  await current.prepare();
  current.restart();
  assert.deepEqual(await (await current.send()).json(), { ok: true });
  assert.deepEqual(current.counts(), { bootstraps: 2, delivered: 1, requests: 2 });
  assert.deepEqual(current.requests.map(r => r.keyId), ['initial', 'replacement']);
  assert(!JSON.stringify(current.requests).includes('PRIVATE_TEST_INPUT'));

  const old = panel(publicKey, { legacy: true });
  await old.prepare();
  old.restart();
  await assert.rejects(old.send(), /输入连接已刷新/);
  assert.deepEqual(old.counts(), { bootstraps: 2, delivered: 0, requests: 1 });
  assert.deepEqual(await (await old.send()).json(), { ok: true });
  assert.deepEqual(old.counts(), { bootstraps: 2, delivered: 1, requests: 2 });

  const uncertain = panel(publicKey, { loseReply: true });
  await uncertain.prepare();
  await assert.rejects(uncertain.send(), /面板调用失败/);
  assert.deepEqual(uncertain.counts(), { bootstraps: 1, delivered: 1, requests: 1 });
  console.log('Native panel key recovery, legacy refresh, and no replay after a lost response passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
