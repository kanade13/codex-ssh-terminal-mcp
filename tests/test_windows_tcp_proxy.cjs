const assert = require('node:assert/strict');
const net = require('node:net');
const { PassThrough } = require('node:stream');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { relay, diagnosticLogger, allowedDestination } = require('../windows-tcp-proxy.cjs');

(async () => {
  assert.equal(allowedDestination('192.0.2.10', 22), false);
  const destinations = [{ host: '192.0.2.10', port: 22 }];
  assert.equal(allowedDestination('192.0.2.10', '22', destinations), true);
  assert.equal(allowedDestination('192.0.2.11', 22, destinations), false);
  assert.equal(allowedDestination('192.0.2.10', 2222, destinations), false);
  const echo = net.createServer(socket => socket.pipe(socket));
  await new Promise(resolve => echo.listen(0, '127.0.0.1', resolve));
  const input = new PassThrough();
  const output = new PassThrough();
  const expected = Buffer.concat([Buffer.from([0, 10, 13, 255, 27]), randomBytes(262144)]);
  const received = [];
  const errors = [];
  const events = [];
  const socket = relay('127.0.0.1', echo.address().port, input, output, error => errors.push(error), event => events.push(event));
  const timeout = setTimeout(() => socket.destroy(Error('test timeout')), 5000);
  try {
    const done = new Promise((resolve, reject) => {
      output.on('data', chunk => received.push(chunk));
      output.once('end', resolve);
      socket.once('error', reject);
    });
    input.end(expected);
    await done;
    assert(Buffer.concat(received).equals(expected), 'relay changed or lost binary payload');
    assert.deepEqual(errors, []);
    assert(events.some(e => e.event === 'connected'));
    assert(events.some(e => e.event === 'input_end'));
    assert(events.some(e => e.event === 'remote_end'));
    assert(events.every(e => !('data' in e) && !('chunk' in e)));
    console.log('Windows TCP relay: 256 KiB binary round trip, backpressure and EOF passed.');
  } finally {
    clearTimeout(timeout);
    socket.destroy();
    await new Promise(resolve => echo.close(resolve));
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ssh-relay-log-test-'));
  try {
    const file = path.join(directory, 'transport-events.jsonl');
    const log = diagnosticLogger(file, '127.0.0.1', 22);
    log({ event: 'socket_error', code: 'ECONNRESET', data: 'DO_NOT_LOG_PAYLOAD', password: 'DO_NOT_LOG_PASSWORD' });
    const text = fs.readFileSync(file, 'utf8');
    assert(text.includes('ECONNRESET'));
    assert(!text.includes('DO_NOT_LOG'));
    fs.writeFileSync(file, 'x'.repeat(1024 * 1024 + 1));
    log({ event: 'connected' });
    assert(fs.existsSync(file + '.1') && fs.statSync(file).size < 4096);
    diagnosticLogger(directory, '127.0.0.1', 22)({ event: 'write_error' });
    console.log('Transport diagnostics: lifecycle events, payload exclusion, bounded rotation and failure isolation passed.');
  } finally {
    // Only known test-created files in this dedicated temporary directory.
    for (const name of ['transport-events.jsonl', 'transport-events.jsonl.1']) {
      if (fs.existsSync(path.join(directory, name))) fs.unlinkSync(path.join(directory, name));
    }
    fs.rmdirSync(directory);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
