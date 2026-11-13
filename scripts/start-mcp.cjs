'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const settingsFile = path.join(__dirname, 'runtime.json');
let settings;
try {
  settings = JSON.parse(fs.readFileSync(settingsFile, 'utf8').replace(/^\uFEFF/, ''));
} catch {
  process.stderr.write('Run Install.ps1 before opening the SSH terminal plugin.\n');
  process.exit(1);
}
const args = [
  '-d', settings.distro, '--exec', 'env',
  `CODEX_SSH_TERMINAL_HOME=${settings.state}`,
  `CODEX_SSH_CONFIG=${settings.state}/ssh_config`,
  `CODEX_SSH_WINDOWS_POWERSHELL=${settings.powershell}`,
  `${settings.runtime}/venv/bin/python`,
  `${settings.runtime}/mcp_server.py`, '--state-dir', settings.state,
];
const child = spawn('wsl.exe', args, { stdio: 'inherit', windowsHide: true });
child.once('error', () => {
  process.stderr.write('Cannot launch the configured WSL runtime. Run Install.ps1 again.\n');
  process.exitCode = 1;
});
child.once('exit', code => { process.exitCode = code ?? 1; });
