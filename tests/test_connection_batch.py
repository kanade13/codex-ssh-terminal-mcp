from pathlib import Path
import threading
import time

import pytest

import connection_batch as batch_module
import server
from test_multi_sessions import post_json, start_manager_server


def configure_fake_connections(monkeypatch, tmp_path):
    profiles = {
        name: {"name": name, "sshHost": name, "sshUser": "alice", "sshPort": 22}
        for name in batch_module.BATCH_PROFILES
    }
    monkeypatch.setattr(batch_module, "get_profile", profiles.get)
    monkeypatch.setattr(batch_module, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(batch_module, "channel_ready", lambda path, name: True)
    monkeypatch.setattr(server.TerminalSession, "start", lambda self: setattr(self, "pid", 123))
    monkeypatch.setattr(server.TerminalSession, "_is_running_locked", lambda self: self.pid is not None)
    return profiles


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def test_targets_start_only_after_master_ready_and_then_run_in_parallel(monkeypatch, tmp_path):
    configure_fake_connections(monkeypatch, tmp_path)
    allow_jump = threading.Event()
    allow_targets = threading.Event()
    monkeypatch.setattr(batch_module, "master_ready", lambda path, name:
                        allow_jump.is_set() if name == "8HFA40" else allow_targets.is_set())
    manager = server.SessionManager()
    result = manager.start_batch()
    wait_for(lambda: len(manager.sessions) == 2)
    assert {s.profile_name for s in manager.sessions.values()} == {"8HFA40", "HF3090"}
    assert manager.start_batch()["connectionBatch"]["id"] == result["connectionBatch"]["id"]
    allow_jump.set()
    wait_for(lambda: len(manager.sessions) == 4)
    assert manager.connection_batch.phase == "connecting"
    assert "8HFA40 已就绪" in manager.connection_batch.status()["message"]
    for session in manager.sessions.values():
        if session.profile_name in ("hf6-A800", "hf7-A40"):
            proxy = next(arg for arg in session.command if arg.startswith("ProxyCommand="))
            assert "BatchMode=yes" in proxy and "-W" in proxy
        elif session.profile_name == "HF3090":
            assert not any(arg.startswith("ProxyCommand=") for arg in session.command)
            assert session.shared_code is None
        if session.profile_name in batch_module.HF_PROFILES:
            assert session.shared_code is manager.connection_batch.code
    allow_targets.set()
    manager.connection_batch.thread.join(3)
    assert manager.connection_batch.phase == "complete"
    assert set(manager.connection_batch.connected) == set(batch_module.BATCH_PROFILES)
    assert manager.connection_batch.code._code is None
    # Clicking again reuses all four sessions rather than creating duplicates.
    manager.start_batch()
    manager.connection_batch.thread.join(3)
    assert manager.connection_batch.phase == "complete"
    assert len(manager.sessions) == 4
    manager.resolve(manager.connection_batch.session_ids["hf7-A40"]).pid = None
    current = manager.connection_batch.status()
    assert "hf7-A40" not in current["connectedProfiles"]
    assert current["phase"] == "partial" and "断开" in current["message"]


def test_failure_on_one_target_keeps_the_other_and_jump_connected(monkeypatch, tmp_path):
    configure_fake_connections(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_module, "master_ready", lambda path, name: True)
    original_start = server.TerminalSession.start

    def start(self):
        if self.profile_name == "hf6-A800":
            raise RuntimeError("simulated start failure")
        original_start(self)

    monkeypatch.setattr(server.TerminalSession, "start", start)
    manager = server.SessionManager()
    manager.start_batch()
    manager.connection_batch.thread.join(3)
    status = manager.connection_batch.status()
    assert status["phase"] == "partial"
    assert set(status["connectedProfiles"]) == {"8HFA40", "hf7-A40", "HF3090"}
    assert "hf6-A800" in status["errors"]
    assert len(manager.sessions) == 3


def test_batch_capacity_is_checked_before_opening_connections(monkeypatch, tmp_path):
    configure_fake_connections(monkeypatch, tmp_path)
    manager = server.SessionManager(max_sessions=3)
    with pytest.raises(ValueError, match="数量不足"):
        manager.start_batch()
    assert not manager.sessions


def test_input_with_invalid_batch_never_reaches_a_shell(tmp_path, monkeypatch):
    manager = server.SessionManager()
    session = server.TerminalSession(["ssh", "example"], session_id="test-session")
    manager.sessions[session.session_id] = session
    writes = []
    monkeypatch.setattr(session, "_write", writes.append)
    httpd, base_url = start_manager_server(tmp_path, manager)
    try:
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as error:
            post_json(base_url + "/api/input", {
                "sessionId": session.session_id, "authBatchId": "missing-batch",
                "data": "123456", "private": True,
            }, {"Authorization": "Bearer test-token"})
        body = error.value.read().decode()
        assert error.value.code == 400 and "123456" not in body
        assert not writes
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_explicit_proxy_replaces_profile_jump_argument(monkeypatch, tmp_path):
    monkeypatch.setattr(batch_module, "state_dir", lambda: tmp_path)
    command, path = batch_module.managed_command("hf6-A800", {
        "sshHost": "192.0.2.6", "sshPort": 22, "sshUser": "alice", "proxyJump": "old-jump",
    }, jump_path="/tmp/private-jump.sock")
    assert "-J" not in command and "old-jump" not in command
    assert "ControlMaster=auto" in command
    assert "IPQoS=none" in command
    proxy = next(arg for arg in command if arg.startswith("ProxyCommand="))
    assert "IPQoS=none" in proxy and "ProxyCommand=false" in proxy
    assert Path(path).parent.stat().st_mode & 0o777 == 0o700


def test_local_master_does_not_imply_usable_remote_channel(monkeypatch, tmp_path):
    configure_fake_connections(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_module, "master_ready", lambda path, name: True)
    monkeypatch.setattr(batch_module, "channel_ready", lambda path, name: False)
    manager = server.SessionManager()
    manager.start_batch()
    manager.connection_batch.thread.join(3)
    result = manager.connection_batch.status()
    assert result["phase"] == "failed" and result["connectedProfiles"] == []
    assert "远端命令通道无响应" in result["errors"]["8HFA40"]
    assert len(manager.sessions) == 2  # 6/7 wait; the password-only host is independent.


def test_password_host_failure_keeps_the_three_mfa_connections(monkeypatch, tmp_path):
    configure_fake_connections(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_module, "master_ready", lambda path, name: True)
    original_start = server.TerminalSession.start

    def start(self):
        if self.profile_name == "HF3090":
            raise RuntimeError("password-only host unavailable")
        original_start(self)

    monkeypatch.setattr(server.TerminalSession, "start", start)
    manager = server.SessionManager()
    manager.start_batch()
    manager.connection_batch.thread.join(3)
    status = manager.connection_batch.status()
    assert status["phase"] == "partial"
    assert set(status["connectedProfiles"]) == set(batch_module.HF_PROFILES)
    assert "HF3090" in status["errors"]


def test_bastion_failure_does_not_prevent_password_host_login(monkeypatch, tmp_path):
    configure_fake_connections(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_module, "master_ready", lambda path, name: True)
    monkeypatch.setattr(batch_module, "channel_ready", lambda path, name: name == "HF3090")
    manager = server.SessionManager()
    manager.start_batch()
    manager.connection_batch.thread.join(3)
    status = manager.connection_batch.status()
    assert status["phase"] == "partial"
    assert status["connectedProfiles"] == ["HF3090"]
    assert set(status["errors"]) == set(batch_module.HF_PROFILES)
    assert len(manager.sessions) == 2


def test_delayed_password_host_does_not_retain_otp_or_hold_up_mfa_logins(monkeypatch, tmp_path):
    configure_fake_connections(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_module, "master_ready", lambda path, name: name != "HF3090")
    manager = server.SessionManager()
    manager.start_batch()
    batch = manager.connection_batch
    try:
        wait_for(lambda: set(batch.connected) == set(batch_module.HF_PROFILES) and batch.code._closed)
        assert batch.phase == "connecting"
        assert not batch.status()["needsCode"]
        assert batch.code._code is None
    finally:
        batch.cancel()
        batch.thread.join(3)
    assert not batch.thread.is_alive()
    assert batch.phase == "cancelled"


def test_missing_fourth_profile_does_not_open_any_connections(monkeypatch, tmp_path):
    profiles = configure_fake_connections(monkeypatch, tmp_path)
    profiles.pop("HF3090")
    manager = server.SessionManager()
    with pytest.raises(ValueError, match="HF3090"):
        manager.start_batch()
    assert not manager.sessions


def test_remote_channel_probe_is_bounded_and_cannot_fall_back_to_auth(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    path = tmp_path / "mux.sock"
    path.touch()
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"__CODEX_SSH_READY__\n")

    monkeypatch.setattr(batch_module.subprocess, "run", run)
    assert batch_module.channel_ready(str(path), "8HFA40")
    args, kwargs = calls[0]
    assert all(option in args for option in ["BatchMode=yes", "ProxyCommand=false", "IPQoS=none"])
    assert kwargs["timeout"] == 8
    monkeypatch.setattr(batch_module.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b""))
    assert not batch_module.channel_ready(str(path), "8HFA40")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 8)
    monkeypatch.setattr(batch_module.subprocess, "run", timeout)
    assert not batch_module.channel_ready(str(path), "8HFA40")
