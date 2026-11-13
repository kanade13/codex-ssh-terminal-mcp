import json
import threading
import urllib.error
import urllib.request

import mcp_server
import server
from transfers import TransferManager


def post_json(url, payload=None, headers=None):
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers


def read_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers


def start_manager_server(tmp_path, manager):
    httpd = server.LocalOnlyHTTPServer(
        ("127.0.0.1", 0),
        server.BridgeHandler,
        manager,
        TransferManager(state_file=tmp_path / "uploads.json"),
        "test-token",
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def make_manager_with_two_sessions():
    manager = server.SessionManager(max_sessions=4)
    first = server.TerminalSession(["ssh", "a"], profile_name="prod-a", session_id="sess_a")
    second = server.TerminalSession(["ssh", "b"], profile_name="prod-b", session_id="sess_b")
    first.writes = []
    second.writes = []
    manager.sessions[first.session_id] = first
    manager.sessions[second.session_id] = second
    return manager, first, second


def test_legacy_agent_route_never_writes_to_shared_terminal(tmp_path, monkeypatch):
    manager, first, second = make_manager_with_two_sessions()

    def fake_write(self, data):
        self.writes.append(data)

    monkeypatch.setattr(server.TerminalSession, "_write", fake_write)
    httpd, base_url = start_manager_server(tmp_path, manager)
    try:
        try:
            post_json(
                base_url + "/api/agent-command",
                {"command": "hostname"},
                {"Authorization": "Bearer test-token"},
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            error = json.loads(exc.read().decode("utf-8"))["error"]
            assert "sessionId is required" in error
        else:
            raise AssertionError("expected missing sessionId to fail when multiple sessions exist")

        try:
            post_json(
                base_url + "/api/agent-command",
                {"sessionId": "sess_a", "expectedProfileName": "prod-a", "command": "exit"},
                {"Authorization": "Bearer test-token"},
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("legacy shared-terminal command accepted")
        assert first.writes == []
        assert second.writes == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_multi_session_profile_guard_rejects_wrong_target(tmp_path, monkeypatch):
    manager, first, second = make_manager_with_two_sessions()

    def fake_write(self, data):
        self.writes.append(data)

    monkeypatch.setattr(server.TerminalSession, "_write", fake_write)
    httpd, base_url = start_manager_server(tmp_path, manager)
    try:
        try:
            post_json(
                base_url + "/api/agent-command",
                {"sessionId": "sess_a", "expectedProfileName": "prod-b", "command": "hostname"},
                {"Authorization": "Bearer test-token"},
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            error = json.loads(exc.read().decode("utf-8"))["error"]
            assert "session/profile mismatch" in error
        else:
            raise AssertionError("expected mismatched profile guard to fail")

        assert first.writes == []
        assert second.writes == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_multi_session_snapshot_requires_explicit_session_id(tmp_path):
    manager, first, second = make_manager_with_two_sessions()
    first._publish("output", "first\n")
    second._publish("output", "second\n")
    httpd, base_url = start_manager_server(tmp_path, manager)
    try:
        try:
            read_json(base_url + "/api/events?format=lines", {"Authorization": "Bearer test-token"})
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            error = json.loads(exc.read().decode("utf-8"))["error"]
            assert "sessionId is required" in error
        else:
            raise AssertionError("expected missing sessionId to fail when reading multiple sessions")

        data, _ = read_json(
            base_url + "/api/events?format=lines&sessionId=sess_a",
            {"Authorization": "Bearer test-token"},
        )

        assert [line["text"] for line in data["lines"]] == ["first"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mcp_cursors_are_isolated_by_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "STATE_FILE", tmp_path / "mcp-state.json")

    mcp_server.update_cursor("http://127.0.0.1:18766", 12, session_id="sess_a")
    mcp_server.update_cursor("http://127.0.0.1:18766", 34, session_id="sess_b")

    assert mcp_server.get_cursor("http://127.0.0.1:18766", "sess_a") == 12
    assert mcp_server.get_cursor("http://127.0.0.1:18766", "sess_b") == 34
    assert mcp_server.get_cursor("http://127.0.0.1:18766", "missing") == 0


def test_session_manager_enforces_four_running_session_limit(monkeypatch):
    def fake_start(self, command=None, profile_name=None):
        if command is not None:
            self.command = command
            self.profile_name = profile_name
        self.pid = 100
        self.returncode = None
        self.started_at = server.now_ms()

    monkeypatch.setattr(server.TerminalSession, "start", fake_start)
    monkeypatch.setattr(server.TerminalSession, "_is_running_locked", lambda self: self.pid is not None and self.returncode is None)

    manager = server.SessionManager(max_sessions=4)
    for index in range(4):
        manager.start_session(["ssh", f"host-{index}"], f"prod-{index}")

    try:
        manager.start_session(["ssh", "host-4"], "prod-4")
    except ValueError as exc:
        assert "maximum running SSH sessions reached: 4" in str(exc)
    else:
        raise AssertionError("expected fifth running session to be rejected")


def test_session_manager_rolls_back_failed_start(monkeypatch):
    def fail_start(self, command=None, profile_name=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(server.TerminalSession, "start", fail_start)
    manager = server.SessionManager(max_sessions=4)

    try:
        manager.start_session(["ssh", "host"], "prod")
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected failed start")

    assert manager.sessions == {}
    assert manager.last_session_id is None


def test_list_sessions_is_compact_and_does_not_include_terminal_history():
    manager, first, second = make_manager_with_two_sessions()
    first._publish("output", "secret-ish terminal output\n")
    second._publish("output", "other terminal output\n")

    listed = manager.list_sessions()

    assert listed["maxSessions"] == 4
    assert listed["sessionCount"] == 2
    assert {item["sessionId"] for item in listed["sessions"]} == {"sess_a", "sess_b"}
    assert all("events" not in item for item in listed["sessions"])
    assert all("history" not in item for item in listed["sessions"])
    assert all("secret-ish terminal output" not in json.dumps(item) for item in listed["sessions"])
