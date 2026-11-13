import time

import pytest

import agent_commands as agent
import mcp_server
import server


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.fixture
def local_commands(monkeypatch, tmp_path):
    calls = []
    real_popen = agent.subprocess.Popen
    profile = {"name": "gpu", "sshHost": "gpu", "sshUser": "alice", "sshPort": 22}
    monkeypatch.setattr(agent, "get_profile", lambda name: profile)
    monkeypatch.setattr(agent, "control_path", lambda *args: str(tmp_path / "private.sock"))
    monkeypatch.setattr(agent, "master_ready", lambda *args: True)

    def local_exec(argv, **kwargs):
        calls.append(argv)
        return real_popen(["/bin/bash", "-c", argv[-1]], **kwargs)

    monkeypatch.setattr(agent.subprocess, "Popen", local_exec)
    session = {"sessionId": "sess_gpu", "profileName": "gpu", "agentInputEnabled": True}
    return agent.AgentCommands(), session, calls


def test_exit_isolated_from_the_users_real_pty(local_commands):
    manager, session, calls = local_commands
    terminal = server.TerminalSession(["/bin/bash", "--noprofile", "--norc", "-i"])
    terminal.start()
    try:
        result = manager.start("printf 'AGENT_ONLY_中文\\n'; exit 7", session)
        job = manager.resolve(result["commandId"])
        job.thread.join(5)
        status = job.status(full=True)
        assert status["returncode"] == 7
        assert "AGENT_ONLY_中文" in str(status["lines"])
        assert "AGENT_ONLY" not in str(terminal.snapshot())
        terminal.write_terminal_input("printf 'USER_SHELL_ALIVE\\n'\r")
        wait_for(lambda: "USER_SHELL_ALIVE" in str(terminal.snapshot()))
        assert terminal.status()["running"]
        assert "ControlMaster=no" in calls[0] and "BatchMode=yes" in calls[0]
        assert "ProxyCommand=false" in calls[0]
    finally:
        terminal.stop()


def test_new_shell_and_other_chat_isolation(local_commands):
    manager, session, calls = local_commands
    first = manager.resolve(manager.start("export AGENT_PRIVATE_VALUE=secret; cd /tmp; pwd", session)["commandId"])
    first.thread.join(5)
    second = manager.resolve(manager.start("printf 'VALUE=%s\\n' \"${AGENT_PRIVATE_VALUE-unset}\"", session)["commandId"])
    second.thread.join(5)
    assert "VALUE=unset" in str(second.status(full=True))
    other_chat = agent.AgentCommands()
    with pytest.raises(ValueError):
        other_chat.resolve(first.id)
    assert len(calls) == 2


def test_dead_master_never_prompts_or_uses_shared_terminal(local_commands, monkeypatch):
    manager, session, calls = local_commands
    monkeypatch.setattr(agent, "master_ready", lambda *args: False)
    with pytest.raises(RuntimeError, match="主连接不可用"):
        manager.start("hostname", session)
    assert not calls


@pytest.mark.parametrize("patch", [
    {"agentInputEnabled": False}, {"authPromptPending": True},
])
def test_private_auth_and_pause_stop_commands(local_commands, patch):
    manager, session, calls = local_commands
    with pytest.raises(PermissionError):
        manager.start("hostname", {**session, **patch})
    assert not calls


def test_profile_guard_precedes_execution(local_commands):
    manager, session, calls = local_commands
    with pytest.raises(ValueError, match="mismatch"):
        manager.start("hostname", session, "wrong-server")
    assert not calls


def test_mcp_works_with_existing_old_bridge_without_legacy_writes(local_commands, monkeypatch):
    manager, session, calls = local_commands
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    tools._agent_commands = manager
    monkeypatch.setattr(tools, "list_sessions", lambda: {"sessions": [session]})
    monkeypatch.setattr(tools, "_post", lambda *args, **kwargs: pytest.fail("writes to old bridge"))
    result = tools.send_command("printf 'INDEPENDENT_CHANNEL\\n'", session["sessionId"])
    manager.resolve(result["commandId"]).thread.join(5)
    assert "INDEPENDENT_CHANNEL" in str(tools.snapshot(session_id=session["sessionId"], full=True))
    assert tools.command_status(result["commandId"], full=True)["returncode"] == 0
    with pytest.raises(PermissionError):
        tools.stop_bridge()
    with pytest.raises(ValueError):
        tools.control("ctrl-c", session_id="someone-elses-session")


def test_cancel_touches_only_own_child_process_group(local_commands):
    manager, session, calls = local_commands
    long_job = manager.resolve(manager.start("sleep 30", session)["commandId"])
    other = manager.resolve(manager.start("sleep 1; printf 'OTHER_JOB_ALIVE\\n'", session)["commandId"])
    assert long_job.cancel()["cancelRequested"]
    long_job.thread.join(5)
    other.thread.join(5)
    assert long_job.returncode is not None
    assert other.returncode == 0
    assert "OTHER_JOB_ALIVE" in str(other.status(full=True))


def test_output_is_bounded_and_failed_commands_are_never_replayed(local_commands):
    manager, session, calls = local_commands
    job = manager.resolve(manager.start("python3 -c 'print(\"x\" * 1200000)'; exit 255", session)["commandId"])
    job.thread.join(5)
    assert job.returncode == 255
    assert len(job.history) <= 256
    assert sum(len(event["text"]) for event in job.history) <= 256 * 4096
    for _ in range(3):
        job.status(peek=True)
    assert len(calls) == 1
