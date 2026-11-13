import json
import threading
import time

import pytest
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import mcp_server
from panel_transport import PanelTransport
from test_multi_sessions import make_manager_with_two_sessions, start_manager_server


class TestClipboard:
    __test__ = False
    def __init__(self):
        self.text = "private-clipboard-test\n中文"
        self.reads = 0
    def read(self):
        self.reads += 1
        return self.text
    def write(self, text):
        self.text = text
    def close(self):
        pass


@pytest.fixture
def transport(tmp_path, monkeypatch):
    manager, first, second = make_manager_with_two_sessions()
    for session in (first, second):
        monkeypatch.setattr(session, "_write", lambda text, s=session: s._publish("output", text))
    httpd, url = start_manager_server(tmp_path, manager)
    tools = mcp_server.TerminalTools(url)
    monkeypatch.setattr(tools, "_auth_token", lambda: "test-token")
    clipboard = TestClipboard()
    gateway = PanelTransport(tools, clipboard)
    yield gateway, first, second, clipboard
    gateway.close()
    httpd.shutdown()
    httpd.server_close()


def client(gateway):
    ws = connect(gateway.descriptor()["url"], proxy=None)
    ws.send(json.dumps({"type": "authenticate", "token": gateway.token}))
    assert json.loads(ws.recv(timeout=2))["type"] == "ready"
    return ws


def receive(ws, predicate):
    while True:
        value = json.loads(ws.recv(timeout=3))
        if predicate(value):
            return value


def test_socket_needs_token_and_rejects_web_origins(transport):
    gateway, _, _, clipboard = transport
    with connect(gateway.descriptor()["url"], proxy=None) as ws:
        ws.send(json.dumps({"type": "authenticate", "token": "incorrect"}))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)
    with pytest.raises(InvalidStatus):
        connect(gateway.descriptor()["url"], origin="https://untrusted.example", proxy=None)
    assert clipboard.reads == 0


def test_live_stream_routes_raw_input_and_detaches_without_stopping_session(transport):
    gateway, first, second, _ = transport
    first._publish("output", "old history")
    with client(gateway) as ws:
        ws.send(json.dumps({"id": 1, "op": "subscribe", "sessionId": first.session_id, "after": 1}))
        assert receive(ws, lambda v: v.get("id") == 1)["result"]["ok"]
        raw = "中文\x1b[A\t\x03\x1b[200~multi\nline\x1b[201~"
        ws.send(json.dumps({"id": 2, "op": "request", "method": "POST", "path": "/api/input", "payload": {
            "sessionId": first.session_id, "data": raw, "terminal": True, "private": True,
        }}))
        event = receive(ws, lambda v: v.get("type") == "event")
        assert event["event"]["text"] == raw
        assert event["sessionId"] == first.session_id
        assert not second.history
        ws.send(json.dumps({"id": 3, "op": "subscribe", "sessionId": second.session_id, "after": 0}))
        receive(ws, lambda v: v.get("id") == 3)
        second._publish("output", "second server")
        event = receive(ws, lambda v: v.get("type") == "event")
        assert event["sessionId"] == second.session_id and event["event"]["text"] == "second server"
    # Detaching the renderer keeps the shared bridge's session objects/history.
    assert first.history[-1]["text"] == raw


def test_burst_stream_does_not_drop_or_reorder_events(transport):
    gateway, first, _, _ = transport
    with client(gateway) as ws:
        ws.send(json.dumps({"id": 1, "op": "subscribe", "sessionId": first.session_id, "after": 0}))
        receive(ws, lambda v: v.get("id") == 1)
        for number in range(500):
            first._publish("output", str(number))
        received = [receive(ws, lambda v: v.get("type") == "event")["event"]["text"] for _ in range(500)]
        assert received == list(map(str, range(500)))


def test_clipboard_only_read_on_explicit_socket_request(transport):
    gateway, _, _, clipboard = transport
    with client(gateway) as ws:
        assert clipboard.reads == 0
        ws.send(json.dumps({"id": 1, "op": "clipboard-read"}))
        result = receive(ws, lambda v: v.get("id") == 1)
        assert result["result"]["text"] == clipboard.text
        assert clipboard.reads == 1
        ws.send(json.dumps({"id": 2, "op": "clipboard-write", "text": "selection"}))
        receive(ws, lambda v: v.get("id") == 2)
        assert clipboard.text == "selection"
        ws.send(json.dumps({"id": 3, "op": "request", "method": "GET", "path": "https://untrusted.example"}))
        assert "error" in receive(ws, lambda v: v.get("id") == 3)
