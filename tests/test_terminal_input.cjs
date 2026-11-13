const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
global.window = {};
vm.runInThisContext(fs.readFileSync(require('node:path').join(__dirname, '../static/terminal-input.js'), 'utf8'));

(async () => {
  const writes = [];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const input = new window.SSHTerminalInput(async (id, data) => {
    if (!writes.length) await gate;
    writes.push([id, data]);
  }, error => { throw error; });
  input.write('six', 'ab');
  const pending = input.drain();
  input.write('seven', '\x1b[A\t\x03\r');
  input.write('six', '中😀'.repeat(3000));
  release();
  await pending;
  assert.deepEqual(writes.slice(0, 2), [['six', 'ab'], ['seven', '\x1b[A\t\x03\r']]);
  assert.equal(writes.slice(2).map(item => item[1]).join(''), '中😀'.repeat(3000));
  assert(writes.slice(2).every(item => item[0] === 'six' && !/[\uD800-\uDBFF]$/.test(item[1])));

  const errors = [];
  const stopped = new window.SSHTerminalInput(async () => { throw Error('offline'); }, error => errors.push(error.message));
  stopped.write('six', 'a');
  stopped.write('seven', 'b');
  await stopped.drain();
  assert.equal(stopped.queue.length, 0);
  stopped.write('seven', 'must not retry');
  assert.equal(stopped.queue.length, 0);
  assert.deepEqual(errors, ['offline']);
  console.log('Terminal input: ordered sessions, escape keys, large Unicode paste, and failure stop passed.');
})();
