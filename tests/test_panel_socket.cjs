const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = vm.createContext({ window: {}, setTimeout, clearTimeout });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/panel-socket.js'), 'utf8'), context);
const writes = [];
let wire;
const socket = new context.window.SSHPanelSocket({ url: 'ws://127.0.0.1:9999/terminal', token: 'test-only' }, {
  socketFactory: () => wire = { send(data) { writes.push(JSON.parse(data)); }, close() {} },
});
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const opening = socket.connect();
  wire.onopen();
  assert.equal(writes[0].type, 'authenticate');
  wire.onmessage({ data: JSON.stringify({ type: 'ready' }) });
  await opening;
  const first = socket.request('request', { path: '/api/input', payload: { data: 'once' } });
  await tick();
  wire.onmessage({ data: JSON.stringify({ id: writes[1].id, result: { ok: true } }) });
  assert.equal((await first).ok, true);
  const second = socket.request('request', { path: '/api/input', payload: { data: 'no replay' } });
  await tick();
  wire.onclose();
  await assert.rejects(second, /输入未重发/);
  assert.equal(writes.filter(value => value.payload?.data === 'no replay').length, 1);
  console.log('WebSocket authentication, response routing, and no automatic input replay passed.');
})();
