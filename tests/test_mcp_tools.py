import json
import urllib.error

import mcp_server
import runtime_config


def tool_names():
    return {tool["name"] for tool in mcp_server.tool_schema()}


def test_tool_schema_exposes_config_server_and_hides_profile_save():
    names = tool_names()

    assert "start_config_server" in names
    assert "runtime_config_status" in names
    assert "set_runtime_config" in names
    assert "list_sessions" in names
    assert "save_profile" not in names

    upload = next(tool for tool in mcp_server.tool_schema() if tool["name"] == "request_upload")
    assert "checksum" in upload["inputSchema"]["properties"]
    send_command = next(tool for tool in mcp_server.tool_schema() if tool["name"] == "send_command")
    assert "session_id" in send_command["inputSchema"]["properties"]
    assert "expected_profile_name" in send_command["inputSchema"]["properties"]


def test_hidden_tool_call_is_rejected():
    server = mcp_server.McpServer(mcp_server.TerminalTools("http://127.0.0.1:18766"))
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "save_profile", "arguments": {}},
        }
    )

    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["error"] == "Unknown tool: save_profile"


def test_bridge_status_reports_token_mismatch(monkeypatch):
    def unauthorized(*args, **kwargs):
        raise urllib.error.HTTPError("http://127.0.0.1:18766/api/status", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(mcp_server, "http_get", unauthorized)

    status = mcp_server.TerminalTools("http://127.0.0.1:18766").bridge_status()

    assert status["reachable"] is True
    assert status["authenticated"] is False
    assert "token" in status["error"]


def test_start_config_server_uses_persisted_runtime(tmp_path, monkeypatch):
    runtime_file = tmp_path / "runtime.json"
    state_file = tmp_path / "mcp-state.json"
    pid_file = tmp_path / "bridge.pid"
    log_file = tmp_path / "bridge.log"
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", runtime_file)
    monkeypatch.setattr(mcp_server, "STATE_FILE", state_file)
    monkeypatch.setattr(mcp_server, "PID_FILE", pid_file)
    monkeypatch.setattr(mcp_server, "LOG_FILE", log_file)
    runtime_config.set_runtime_config_payload("python", python_path="/opt/python/bin/python")

    calls = {}

    class FakeProc:
        pid = 4321

    def fake_popen(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(mcp_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mcp_server, "wait_for_bridge", lambda base_url, token=None: {"running": False, "baseUrl": base_url})
    monkeypatch.setattr(mcp_server.TerminalTools, "bridge_status", lambda self: {"reachable": False})

    result = mcp_server.TerminalTools("http://127.0.0.1:18766").start_config_server(port=18888)

    assert result["ok"] is True
    assert result["pid"] == 4321
    assert result["browserUrl"] == "http://127.0.0.1:18888/"
    assert calls["args"][:2] == ["/opt/python/bin/python", str(mcp_server.ROOT / "server.py")]
    assert "--no-auto-start" in calls["args"]
    assert "--api-token" not in calls["args"]
    assert calls["kwargs"]["env"]["CODEX_SSH_TERMINAL_TOKEN"]
    assert calls["kwargs"]["env"]["CODEX_SSH_TERMINAL_HOME"]
    assert pid_file.read_text().strip() == "4321"
    state = json.loads(state_file.read_text())
    assert state["http://127.0.0.1:18888"]["authToken"]


def test_runtime_config_status_guides_first_time_user(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "runtime.json")

    status = runtime_config.runtime_config_status()

    assert status["configured"] is False
    assert "set_runtime_config" in status["hint"]


def test_rotate_log_file_limits_backups(tmp_path):
    log_file = tmp_path / "bridge.log"
    log_file.write_text("new log content")
    (tmp_path / "bridge.log.1").write_text("older 1")
    (tmp_path / "bridge.log.2").write_text("older 2")

    rotated = mcp_server.rotate_log_file(log_file, max_bytes=4, backups=2)

    assert rotated is True
    assert not log_file.exists()
    assert (tmp_path / "bridge.log.1").read_text() == "new log content"
    assert (tmp_path / "bridge.log.2").read_text() == "older 1"
    assert not (tmp_path / "bridge.log.3").exists()


def test_rotate_log_file_skips_small_file(tmp_path):
    log_file = tmp_path / "bridge.log"
    log_file.write_text("small")

    rotated = mcp_server.rotate_log_file(log_file, max_bytes=100, backups=2)

    assert rotated is False
    assert log_file.read_text() == "small"
