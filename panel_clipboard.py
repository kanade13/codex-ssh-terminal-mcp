"""On-demand clipboard operations for explicit terminal copy/paste gestures.

Only the authenticated UI WebSocket can call this adapter. Clipboard text never
travels through an MCP tool argument/result or the terminal's command history.
"""
import base64
import json
import os
from pathlib import Path
import select
import subprocess
import threading


_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
while ($null -ne ($line = [Console]::ReadLine())) {
    try {
        $request = ConvertFrom-Json -InputObject $line
        if ($request.op -eq 'read') {
            $text = [System.Windows.Forms.Clipboard]::GetText()
            if ($text.Length -gt 524288) { throw 'Clipboard too large' }
            $answer = @{ok=$true; data=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))}
        } elseif ($request.op -eq 'write') {
            $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($request.data))
            if ($text.Length -gt 0) { [System.Windows.Forms.Clipboard]::SetText($text) }
            $answer = @{ok=$true}
        } else { throw 'Unsupported operation' }
        [Console]::WriteLine(($answer | ConvertTo-Json -Compress))
    } catch { [Console]::WriteLine('{"ok":false}') }
}
"""


class LocalClipboard:
    def __init__(self):
        self.process = None
        self.lock = threading.Lock()

    def _request(self, operation, text=""):
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Clipboard text is too large")
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                executable = Path(os.environ.get("CODEX_SSH_WINDOWS_POWERSHELL", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))
                if os.name == "nt":
                    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
                if not executable.is_file():
                    raise RuntimeError("Use the browser clipboard on this platform")
                encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
                self.process = subprocess.Popen(
                    [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            process = self.process
            request = {"op": operation, "data": base64.b64encode(text.encode("utf-8")).decode("ascii")}
            process.stdin.write((json.dumps(request) + "\n").encode("ascii"))
            process.stdin.flush()
            # The adapter is installed in WSL; a bounded read prevents a hung
            # desktop clipboard owner from blocking subsequent terminal input.
            if os.name != "nt" and not select.select([process.stdout], [], [], 8)[0]:
                process.kill()
                raise TimeoutError("Clipboard is unavailable")
            line = process.stdout.readline(2 * 1024 * 1024)
            answer = json.loads(line)
            if answer.get("ok") is not True:
                raise RuntimeError("Clipboard is unavailable")
            return base64.b64decode(answer.get("data", "")).decode("utf-8")

    def read(self):
        return self._request("read")

    def write(self, text):
        self._request("write", text)

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
