"""Authenticated loopback WebSocket for the native terminal, independent of SSH.

The MCP process owns this listener. Closing a panel only detaches its event stream;
the existing bridge and its SSH processes are never stopped by this transport.
"""
import hmac
import json
import logging
import re
import secrets
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

# The local installer uses an isolated dependency directory, not system Python.
_vendor = Path(__file__).resolve().parent / "vendorpy"
if _vendor.is_dir():
    sys.path.insert(0, str(_vendor))

from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed

from panel_clipboard import LocalClipboard


class PanelTransport:
    def __init__(self, tools, clipboard=None):
        self.tools = tools
        self.token = secrets.token_urlsafe(32)
        self.clipboard = clipboard or LocalClipboard()
        self.clients = set()
        self.lock = threading.Lock()
        logger = logging.getLogger("ssh-panel-websocket")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False  # Never log request frames or clipboard contents.
        self.server = serve(
            self.handle, "127.0.0.1", 0,
            origins=[None, "null", re.compile(
                r"(?:codex-sandbox|https)://mcp-app-[a-zA-Z0-9-]+\.web-sandbox\.oaiusercontent\.com"
            )],
            compression=None, max_size=2 * 1024 * 1024, max_queue=16,
            open_timeout=5, close_timeout=1, ping_interval=20, ping_timeout=20,
            logger=logger,
        )
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self):
        return f"ws://127.0.0.1:{self.port}"

    def descriptor(self):
        # UI metadata only: never include this capability in model-facing content.
        return {"url": self.origin + "/terminal", "token": self.token, "version": 1}

    def close(self):
        self.server.shutdown()
        with self.lock:
            clients = list(self.clients)
        for ws in clients:
            ws.close()
        self.clipboard.close()

    def handle(self, ws):
        stream = None
        try:
            if ws.request.path != "/terminal":
                ws.close(1008, "Invalid route")
                return
            hello = json.loads(ws.recv(timeout=5))
            token = hello.get("token", "")
            if hello.get("type") != "authenticate" or not isinstance(token, str) or not hmac.compare_digest(token, self.token):
                ws.close(1008, "Unauthorized")
                return
            with self.lock:
                self.clients.add(ws)
            ws.send(json.dumps({"type": "ready"}))
            for raw in ws:
                request_id = None
                try:
                    value = json.loads(raw)
                    request_id = value["id"]
                    if not isinstance(request_id, int):
                        raise ValueError()
                    op = value.get("op")
                    if op == "subscribe":
                        if stream:
                            stream.close()
                        stream = EventStream(self.tools, ws, value["sessionId"], value.get("after", 0))
                        stream.start()
                        result = {"ok": True}
                    elif op == "unsubscribe":
                        if stream:
                            stream.close()
                            stream = None
                        result = {"ok": True}
                    elif op == "request":
                        path, method = value.get("path"), value.get("method")
                        payload = value.get("payload") or {}
                        if method == "POST" and path in ("/api/input", "/api/upload-input"):
                            result = self.tools._post(path, payload, timeout=10)
                        else:
                            result = self.tools.panel_request(method, path, payload)["_meta"]["payload"]
                    elif op == "clipboard-read":
                        result = {"text": self.clipboard.read()}
                    elif op == "clipboard-write":
                        self.clipboard.write(value.get("text", ""))
                        result = {"ok": True}
                    elif op == "ping":
                        result = {"ok": True}
                    else:
                        raise ValueError()
                    ws.send(json.dumps({"id": request_id, "result": result}, ensure_ascii=False))
                except ConnectionClosed:
                    break
                except Exception:
                    # Do not reflect arguments, clipboard contents, or auth inputs.
                    ws.send(json.dumps({"id": request_id, "error": "本机终端操作未完成，请检查连接后重试。"}))
        except (ConnectionClosed, TimeoutError, ValueError, KeyError, TypeError):
            pass
        finally:
            if stream:
                stream.close()
            with self.lock:
                self.clients.discard(ws)


class EventStream:
    """Reuse the bridge's existing push stream; no polling or SSH reconnect."""
    def __init__(self, tools, ws, session_id, after):
        if not isinstance(session_id, str) or not isinstance(after, int) or after < 0:
            raise ValueError("Invalid subscription")
        self.tools, self.ws, self.session_id, self.after = tools, ws, session_id, after
        self.stopped = threading.Event()
        self.response = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        # Closing a blocking HTTP reader can wait for its heartbeat. Do this off
        # the input thread, so switching tabs never stalls keystrokes for 15 s.
        if self.response:
            threading.Thread(target=self.response.close, daemon=True).start()

    def run(self):
        try:
            params = urllib.parse.urlencode({"sessionId": self.session_id, "history": 6000})
            request = urllib.request.Request(
                self.tools.base_url + "/events?" + params,
                headers={"Authorization": "Bearer " + self.tools._auth_token()},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=25) as response:
                self.response = response
                for line in response:
                    if self.stopped.is_set():
                        return
                    if not line.startswith(b"data: "):
                        continue
                    event = json.loads(line[6:])
                    if event.get("seq", 0) <= self.after:
                        continue
                    self.after = event["seq"]
                    self.ws.send(json.dumps({
                        "type": "event", "sessionId": self.session_id, "event": event,
                    }, ensure_ascii=False))
        except Exception:
            if not self.stopped.is_set():
                try:
                    self.ws.send(json.dumps({"type": "stream-error", "sessionId": self.session_id}))
                except ConnectionClosed:
                    pass
