'use strict';
// Raw TCP transport for WSL OpenSSH through the Windows network stack.
// SSH still performs encryption and host-key/authentication checks end to end.
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');

function relay(host, port, input, output, reportError = () => {}, diagnostic = () => {}) {
  const started = Date.now();
  let sent = 0, received = 0, lastData = started;
  const note = (event, code) => {
    try { diagnostic({ event, code, sent, received, elapsedMs: Date.now() - started, idleMs: Date.now() - lastData }); }
    catch { /* Diagnostics must never interrupt SSH. */ }
  };
  note('start');
  const socket = net.createConnection({ host, port });
  const connectTimer = setTimeout(() => {
    note('connect_timeout', 'ETIMEDOUT');
    socket.destroy(Error('connect timeout'));
  }, 20000);
  let closed = false;
  const finish = () => {
    if (closed) return;
    closed = true;
    clearTimeout(connectTimer);
    input.unpipe(socket);
    socket.destroy();
    input.destroy();
  };
  socket.once('connect', () => {
    clearTimeout(connectTimer);
    socket.setNoDelay(true);
    socket.setKeepAlive(true, 30000);
    note('connected');
    input.pipe(socket);
    // Adding a data listener before connect would drain buffered SSH input.
    input.on('data', chunk => { sent += chunk.length; lastData = Date.now(); });
    socket.pipe(output);
  });
  socket.on('error', error => {
    note('socket_error', error.code);
    reportError(error.code || 'connection failed');
    finish();
  });
  socket.once('close', hadError => { note('socket_close', hadError ? 'ERROR' : 'NORMAL'); finish(); });
  socket.on('data', chunk => { received += chunk.length; lastData = Date.now(); });
  input.once('end', () => note('input_end'));
  socket.once('end', () => note('remote_end'));
  input.on('error', error => { note('input_error', error.code); finish(); });
  output.on('error', error => { note('output_error', error.code); finish(); });
  return socket;
}

function diagnosticLogger(file, host, port) {
  const fields = ['event', 'sent', 'received', 'elapsedMs', 'idleMs', 'exitCode'];
  return details => {
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      if (fs.existsSync(file) && fs.statSync(file).size > 1024 * 1024) {
        try { fs.unlinkSync(file + '.1'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
        fs.renameSync(file, file + '.1');
      }
      const record = { ts: new Date().toISOString(), pid: process.pid, ppid: process.ppid, host, port };
      for (const key of fields) if (details[key] !== undefined) record[key] = details[key];
      if (details.code) record.code = /^[A-Z][A-Z0-9_]{0,40}$/.test(details.code) ? details.code : 'OTHER';
      fs.appendFileSync(file, JSON.stringify(record) + '\n', { mode: 0o600 });
    } catch { /* A full disk or inaccessible log must not disconnect SSH. */ }
  };
}

function allowedDestination(host, port, destinations = []) {
  return destinations.some(item => item.host === host && String(item.port) === String(port));
}

module.exports = { relay, allowedDestination, diagnosticLogger };
if (require.main === module) {
  const [host, port, configFile] = process.argv.slice(2);
  let destinations = [];
  try { destinations = JSON.parse(fs.readFileSync(configFile, 'utf8')); } catch {}
  if (!Array.isArray(destinations) || !allowedDestination(host, port, destinations)) {
    process.stderr.write('SSH transport: destination is not a configured server.\n');
    process.exitCode = 1;
  } else {
    const diagnostic = diagnosticLogger(path.join(__dirname, 'transport-events.jsonl'), host, Number(port));
    process.once('exit', exitCode => diagnostic({ event: 'process_exit', exitCode }));
    relay(host, Number(port), process.stdin, process.stdout, code => {
      process.stderr.write(`SSH TCP transport failed: ${code}\n`);
      process.exitCode = 1;
    }, diagnostic);
  }
}
