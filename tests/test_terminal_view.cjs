const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Terminal {
  constructor(options) { this.options = options; this.pasted = []; this.selection = ''; }
  open() {} dispose() {} loadAddon() {} onData() {} onResize() {} focus() {}
  attachCustomKeyEventHandler(handler) { this.key = handler; }
  hasSelection() { return !!this.selection; }
  getSelection() { return this.selection; }
  paste(text) { this.pasted.push(text); }
}
const calls = [];
const window = { Terminal, FitAddon: { FitAddon: class { fit() {} } }, codexSSHNative: {
  readClipboard: async () => '中文\r\nsecond line',
  writeClipboard: async text => calls.push(text),
} };
const context = vm.createContext({ window, navigator: { clipboard: {
  readText: async () => { throw new Error('Iframe denied'); },
  writeText: async () => { throw new Error('Iframe denied'); },
} }, ResizeObserver: class { observe() {} }, requestAnimationFrame: () => 1 });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/terminal-view.js'), 'utf8'), context);
const handlers = {};
const element = { replaceChildren() {}, addEventListener(name, handler) { handlers[name] = handler; } };
const notices = [];
const view = new window.SSHOutputTerminal(element, { onNotice: message => notices.push(message) });
const tick = () => new Promise(resolve => setImmediate(resolve));
const event = extra => ({ type: 'keydown', key: '', preventDefault() {}, stopImmediatePropagation() {}, ...extra });
(async () => {
  // No clipboard read on mount; right-click uses the same xterm paste path.
  view.setEnabled(true);
  handlers.contextmenu(event());
  await tick();
  assert.deepEqual(view.terminal.pasted, ['中文\r\nsecond line']);
  assert.equal(view.terminal.key(event({ ctrlKey: true, key: 'c' })), true);
  view.terminal.selection = 'selection copied';
  assert.equal(view.terminal.key(event({ ctrlKey: true, key: 'c' })), false);
  await tick();
  assert.deepEqual(calls, ['selection copied']);
  view.terminal.key(event({ ctrlKey: true, shiftKey: true, key: 'v' }));
  view.terminal.key(event({ type: 'keyup', ctrlKey: true, shiftKey: true, key: 'v' }));
  await tick();
  assert.equal(view.terminal.pasted.length, 2);
  view.terminal.key(event({ ctrlKey: true, key: 'v' }));
  view.terminal.key(event({ type: 'keyup', ctrlKey: true, key: 'v' }));
  await tick();
  assert.equal(view.terminal.pasted.length, 3);
  let release;
  window.codexSSHNative.readClipboard = () => new Promise(resolve => { release = resolve; });
  const pending = view.pasteClipboard();
  await tick();
  view.reset(); // Switched servers before the clipboard response returned.
  release('must not enter the other server');
  await pending;
  assert.deepEqual(view.terminal.pasted, []);
  view.setEnabled(false);
  handlers.contextmenu(event());
  assert.deepEqual(view.terminal.pasted, []);
  assert.deepEqual(notices, []);
  delete window.codexSSHNative;
  assert.equal(view.terminal.key(event({ ctrlKey: true, key: 'v' })), true); // Preserve standalone browser paste.
  console.log('Clipboard gestures, Ctrl+C interrupt/copy, multiline paste, keyup deduplication, and tab-switch isolation passed.');
})();
