import json
import os
import time

import pytest

import mcp_panel
import mcp_server


def test_panel_bootstrap_never_exposes_bridge_token(monkeypatch):
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    starts = []
    monkeypatch.setattr(tools, "start_config_server", lambda port: starts.append(port) or {"status": {}})
    monkeypatch.setattr(tools, "_auth_token", lambda: "panel-test-token")
    response = mcp_server.McpServer(tools).handle({
        "id": 1, "method": "tools/call",
        "params": {"name": "open_ssh_panel", "arguments": {}},
    })["result"]
    assert starts == [18766]
    assert "panel-test-token" not in json.dumps(response["content"])
    assert "panel-test-token" not in json.dumps(response["structuredContent"])
    assert "panel-test-token" not in json.dumps(response)
    assert response["_meta"]["sshBridge"]["transport"] == "mcp"
    assert response["_meta"]["sshBridge"]["panelVersion"] == mcp_panel.PANEL_VERSION
    assert response["_meta"]["sshBridge"]["privateClipboard"] is True
    assert response["_meta"]["sshBridge"]["encryption"]["publicKey"]


def test_panel_rejects_bridge_owned_by_different_configuration(monkeypatch):
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    monkeypatch.setattr(tools, "start_config_server", lambda port: {"status": {"authenticated": False}})
    with pytest.raises(RuntimeError, match="different local configuration"):
        tools.panel_bootstrap()


@pytest.mark.parametrize("url", [
    "https://example.com:18766", "http://127.0.0.1.evil.example:18766",
    "http://user:password@127.0.0.1:18766", "http://127.0.0.1:18766/?token=bad",
    "http://127.0.0.1:18766/private", "http://127.0.0.1",
])
def test_panel_never_sends_local_token_to_external_origin(url):
    with pytest.raises(ValueError):
        mcp_panel.read_panel_resource(url)


def test_ui_resource_is_available_without_starting_bridge(monkeypatch):
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    monkeypatch.setattr(tools, "start_config_server", lambda **kwargs: pytest.fail("resource started SSH"))
    server = mcp_server.McpServer(tools)
    response = server.handle({
        "id": 1, "method": "resources/read", "params": {"uri": mcp_panel.PANEL_URI},
    })
    resource = response["result"]["contents"][0]
    assert resource["mimeType"] == mcp_panel.PANEL_MIME
    assert resource["_meta"]["ui"]["csp"]["connectDomains"] == ["ws://127.0.0.1:*"]
    assert "__BRIDGE_ORIGIN_JSON__" not in resource["text"]
    assert "ui/initialize" in resource["text"]
    assert "terminal: true, private: true" in resource["text"]
    missing = server.handle({"id": 2, "method": "resources/read", "params": {"uri": "file:///etc/passwd"}})
    assert missing["error"]["code"] == -32602


def test_panel_has_user_entrypoints_and_private_bootstrap():
    definitions = {tool["name"]: tool for tool in mcp_server.tool_schema()}
    entry = definitions["open_ssh_panel"]["_meta"]
    assert entry["openai/ui"]["entrypoints"] == [{"type": "global"}, {"type": "thread"}]
    assert entry["ui"]["resourceUri"] == mcp_panel.PANEL_URI
    assert definitions["panel_bootstrap"]["_meta"]["ui"]["visibility"] == ["app"]
    # No tool accepts a password or a verification code.
    assert definitions["panel_bootstrap"]["inputSchema"]["properties"] == {}


@pytest.mark.parametrize("path", ["/api/input", "/api/upload-input", "https://evil.example/api/status", "/api/agent-command"])
def test_panel_data_channel_rejects_private_input_and_unknown_routes(path):
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    with pytest.raises(ValueError):
        tools.panel_request("POST", path, {"data": "must-not-be-sent"})


def test_panel_data_channel_returns_state_only_to_ui(monkeypatch):
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    monkeypatch.setattr(tools, "_get", lambda path, timeout: {"sessions": [{"sessionId": "test"}]})
    result = tools.panel_request("GET", "/api/status")
    assert result["content"] == []
    assert result["_meta"]["payload"]["sessions"][0]["sessionId"] == "test"


def test_panel_reports_live_compat_maintenance_without_private_process_metadata(monkeypatch, tmp_path):
    report = tmp_path / "maintenance.json"
    report.write_text(json.dumps({"running": True, "pid": os.getpid(), "expiresAt": time.time() * 1000 + 60000,
                                  "version": "0.8.0", "heldProfiles": ["gpu"], "message": "ready"}))
    monkeypatch.setattr(mcp_server, "state_file", lambda *args, **kwargs: report)
    tools = mcp_server.TerminalTools("http://127.0.0.1:18766")
    monkeypatch.setattr(tools, "_get", lambda *args, **kwargs: {"sessions": [], "features": {}})
    response = tools.panel_request("GET", "/api/status")
    assert response["content"] == []
    value = response["_meta"]["payload"]
    assert value["features"]["connectionMaintenanceCompat"] is True
    assert value["maintenance"]["heldProfiles"] == ["gpu"]
    assert "pid" not in value["maintenance"]
    report.write_text(json.dumps({"running": True, "pid": os.getpid(), "expiresAt": 1}))
    assert tools.maintenance_status() is None
