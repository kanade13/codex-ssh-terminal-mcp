import pytest
import os
import time

import connection_batch
import connection_recovery as recovery
import server


@pytest.fixture
def disconnected(monkeypatch, tmp_path):
    manager = server.SessionManager()
    session = server.TerminalSession(["ssh", "gpu"], profile_name="hf6-A800", control_socket=str(tmp_path / "mux.sock"))
    session.authenticated = True
    session._publish("output", "ORIGINAL_JOB_OUTPUT\n")
    manager.sessions[session.session_id] = session
    starts = []

    def start(self, command=None, profile_name=None):
        starts.append(command)
        self.pid, self.returncode = 123, None

    monkeypatch.setattr(server.TerminalSession, "start", start)
    monkeypatch.setattr(server.TerminalSession, "_is_running_locked", lambda self: self.pid is not None)
    monkeypatch.setattr(session, "_write", lambda *args: pytest.fail("replayed terminal input"))
    monkeypatch.setattr(recovery, "master_ready", lambda *args: True)
    monkeypatch.setattr(recovery, "channel_ready", lambda *args: True)
    monkeypatch.setattr(recovery, "get_profile", lambda name: {"name": name, "sshHost": "gpu", "sshPort": 22})
    return manager, session, starts, recovery.ConnectionRecovery(manager)


def test_restore_keeps_session_id_history_and_never_replays_commands(disconnected):
    manager, session, starts, worker = disconnected
    original_id = session.session_id
    original_command = list(session.command)
    worker.tick(now=0)
    assert session.session_id == original_id
    assert session.command == original_command
    assert session.authenticated and session.recovery_state == "ready"
    assert "ORIGINAL_JOB_OUTPUT" in str(session.snapshot())
    assert len(starts) == 1 and "ControlMaster=no" in starts[0]
    assert "BatchMode=yes" in starts[0] and "ProxyCommand=false" in starts[0]
    worker.tick(now=10)
    assert len(starts) == 1


def test_user_disconnect_is_not_undone(disconnected):
    manager, session, starts, worker = disconnected
    session.stop()
    worker.tick(now=0)
    assert not starts and not session.auto_reconnect


def test_disconnect_during_probe_cannot_race_reconnect(disconnected, monkeypatch):
    manager, session, starts, worker = disconnected
    def probe(*args):
        session.stop()
        return True
    monkeypatch.setattr(recovery, "channel_ready", probe)
    worker.tick(now=0)
    assert not starts


def test_lost_mfa_master_requests_new_auth_without_password_or_otp_replay(disconnected, monkeypatch):
    manager, session, starts, worker = disconnected
    monkeypatch.setattr(recovery, "master_ready", lambda *args: False)
    worker.tick(now=0)
    assert not starts
    assert session.recovery_state == "needs_auth"
    assert "重新连接" in session.recovery_message
    assert session.shared_code is None


def test_unresponsive_master_uses_backoff_and_never_kills_live_tasks(disconnected, monkeypatch):
    manager, session, starts, worker = disconnected
    calls = []
    monkeypatch.setattr(recovery, "channel_ready", lambda *args: calls.append(args) or False)
    for now in (0, 1, 4, 5, 6, 14, 15):
        worker.tick(now=now)
    assert len(calls) == 3 and not starts
    assert session.recovery_state == "retrying"


def test_restore_does_not_evict_other_connections(disconnected):
    manager, session, starts, worker = disconnected
    for index in range(4):
        live = server.TerminalSession(["ssh", "other"], profile_name=str(index))
        live.pid = 100 + index
        manager.sessions[live.session_id] = live
    worker.tick(now=0)
    assert not starts
    assert sum(s.status()["running"] for s in manager.sessions.values()) == 4


def test_old_session_is_not_restored_after_user_replaced_it(disconnected):
    manager, session, starts, worker = disconnected
    newer = server.TerminalSession(["ssh", "gpu"], profile_name=session.profile_name, control_socket=session.control_path)
    newer.auto_reconnect = False
    manager.sessions[newer.session_id] = newer
    worker.tick(now=0)
    assert not starts


def test_password_only_host_gets_one_relogin_attempt_not_an_auth_loop(disconnected, monkeypatch):
    manager, session, starts, worker = disconnected
    session.profile_name = connection_batch.PASSWORD_PROFILE
    monkeypatch.setattr(recovery, "master_ready", lambda *args: False)
    worker.tick(now=0)
    assert len(starts) == 1 and not session.authenticated
    session.pid = None  # password/login failed
    worker.tick(now=20)
    assert len(starts) == 1 and session.recovery_state == "needs_auth"


def test_control_persistence_and_channel_reuse_are_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(connection_batch, "state_dir", lambda: tmp_path)
    profile = {"sshHost": "gpu", "proxyJump": "jump", "sshUser": "alice"}
    command, path = connection_batch.managed_command("gpu", profile)
    assert "ControlPersist=43200" in command
    command = connection_batch.reuse_command(profile, path, remote_command="exit 0")
    assert "-J" not in command and "jump" not in command
    assert "ControlMaster=no" in command and "BatchMode=yes" in command
    assert "ProxyCommand=false" in command and "ClearAllForwardings=yes" in command
    assert command[-1] == "exit 0"


def test_real_pty_recovery_discards_closed_otp_pool_and_never_opens_askpass(monkeypatch, tmp_path):
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text('#!/bin/sh\nexec /bin/bash --noprofile --norc -i\n')
    fake_ssh.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(server.AskpassBroker, "for_profile", lambda *args: pytest.fail("mux restore opened askpass"))
    monkeypatch.setattr(recovery, "master_ready", lambda *args: True)
    monkeypatch.setattr(recovery, "channel_ready", lambda *args: True)
    monkeypatch.setattr(recovery, "get_profile", lambda name: {"name": name, "sshHost": "isolated-test"})
    manager = server.SessionManager()
    closed_pool = connection_batch.SharedLoginCode()
    closed_pool.close()
    session = server.TerminalSession(["ssh", "isolated-test"], profile_name="hf6-A800",
                                     control_socket=str(tmp_path / "test.sock"), shared_code=closed_pool)
    session.authenticated = True
    manager.sessions[session.session_id] = session
    worker = recovery.ConnectionRecovery(manager)
    try:
        worker.tick(now=0)
        assert session.shared_code is None and session.auth_broker is None
        assert session.status()["connected"]
        session.write_terminal_input("printf 'RECOVERED_PTY_ALIVE\\n'\r")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "RECOVERED_PTY_ALIVE" not in str(session.snapshot()):
            time.sleep(0.01)
        assert "RECOVERED_PTY_ALIVE" in str(session.snapshot())
    finally:
        session.stop()
        if session.status()["running"]:
            import signal
            try:
                os.killpg(session.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if session.reader_thread:
            session.reader_thread.join(3)
            assert not session.reader_thread.is_alive()
