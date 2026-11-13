from pathlib import Path

import pytest

import panel_clipboard


@pytest.mark.skipif(not Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe').is_file(), reason='WSL clipboard adapter')
def test_windows_worker_roundtrips_unicode_without_accessing_real_clipboard(monkeypatch):
    # Exercise the exact Windows subprocess/framing path with a private memory
    # clipboard. Never read or replace the user's actual Windows clipboard.
    script = panel_clipboard._SCRIPT.replace(
        'Add-Type -AssemblyName System.Windows.Forms', "$script:testClipboard = ''",
    ).replace('[System.Windows.Forms.Clipboard]::GetText()', '$script:testClipboard').replace(
        '[System.Windows.Forms.Clipboard]::SetText($text)', '$script:testClipboard = $text',
    )
    monkeypatch.setattr(panel_clipboard, '_SCRIPT', script)
    clipboard = panel_clipboard.LocalClipboard()
    try:
        text = '中文😀\r\n$() `hello` "quoted"\nsecond line'
        clipboard.write(text)
        assert clipboard.read() == text
        worker = clipboard.process
        clipboard.write('another copy')
        assert clipboard.read() == 'another copy'
        assert clipboard.process is worker
        assert text not in ' '.join(worker.args)
    finally:
        clipboard.close()
