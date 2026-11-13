from types import SimpleNamespace

import pytest

import profile_secrets


def test_secret_tool_store_reports_secret_service_setup(monkeypatch):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Cannot autolaunch D-Bus without X11 $DISPLAY",
        )

    monkeypatch.setattr(profile_secrets.subprocess, "run", fake_run)
    backend = profile_secrets.SecretToolBackend()

    with pytest.raises(profile_secrets.SecretBackendUnavailable) as exc:
        backend.store(
            {"service": "codex-ssh-terminal-mcp", "account": "profile:test"},
            "{}",
        )

    message = str(exc.value)
    assert "Linux Secret Service is unavailable" in message
    assert "GNOME Keyring or KWallet" in message
    assert "WSL" in message
