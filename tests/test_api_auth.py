import json
import threading
import urllib.error
import urllib.request

import server
from transfers import TransferManager


def start_test_server(tmp_path):
    httpd = server.LocalOnlyHTTPServer(
        ("127.0.0.1", 0),
        server.BridgeHandler,
        server.TerminalSession(),
        TransferManager(state_file=tmp_path / "uploads.json"),
        "test-token",
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def read_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers


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


def test_api_requires_token_and_accepts_authorization_header(tmp_path):
    httpd, base_url = start_test_server(tmp_path)
    try:
        try:
            read_json(base_url + "/api/status")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("expected unauthorized API request to fail")

        data, _ = read_json(base_url + "/api/status", {"Authorization": "Bearer test-token"})

        assert data["running"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_bad_host_header_is_rejected(tmp_path):
    httpd, base_url = start_test_server(tmp_path)
    try:
        try:
            read_json(
                base_url + "/api/status",
                {
                    "Authorization": "Bearer test-token",
                    "Host": "evil.example",
                },
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("expected bad Host request to fail")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cookie_post_requires_same_origin_or_referer(tmp_path):
    httpd, base_url = start_test_server(tmp_path)
    try:
        req = urllib.request.Request(base_url + "/")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            cookie = resp.headers["Set-Cookie"].split(";", 1)[0]

        try:
            post_json(base_url + "/api/agent-enabled", {"enabled": True}, {"Cookie": cookie})
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("expected cookie POST without Origin/Referer to fail")

        try:
            post_json(
                base_url + "/api/agent-enabled",
                {"enabled": True},
                {
                    "Cookie": cookie,
                    "Origin": "http://evil.example",
                },
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("expected cookie POST with bad Origin to fail")

        data, _ = post_json(
            base_url + "/api/agent-enabled",
            {"enabled": True},
            {
                "Cookie": cookie,
                "Origin": base_url,
            },
        )

        assert data["agentInputEnabled"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_bearer_post_does_not_require_browser_origin(tmp_path):
    httpd, base_url = start_test_server(tmp_path)
    try:
        data, _ = post_json(
            base_url + "/api/agent-enabled",
            {"enabled": True},
            {"Authorization": "Bearer test-token"},
        )

        assert data["agentInputEnabled"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_root_sets_auth_cookie_for_browser_api_and_events(tmp_path):
    httpd, base_url = start_test_server(tmp_path)
    try:
        req = urllib.request.Request(base_url + "/")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as resp:
            cookie = resp.headers["Set-Cookie"].split(";", 1)[0]

        data, _ = read_json(base_url + "/api/status", {"Cookie": cookie})

        assert data["running"] is False
        assert cookie == "codex_ssh_bridge_token=test-token"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_polling_paths_are_not_access_logged(capsys):
    handler = server.BridgeHandler.__new__(server.BridgeHandler)
    handler.client_address = ("127.0.0.1", 12345)
    handler.log_date_time_string = lambda: "now"
    handler.path = "/api/status"

    handler.log_message('"GET %s HTTP/1.1" 200 -', handler.path)

    assert capsys.readouterr().out == ""


def test_http_bridge_does_not_allow_cross_origin_access(tmp_path):
    httpd, base_url = start_test_server(tmp_path)
    try:
        data, headers = read_json(base_url + "/api/status", {
            "Origin": "null", "Authorization": "Bearer test-token",
        })
        assert data["running"] is False
        assert "Access-Control-Allow-Origin" not in headers
        assert "Access-Control-Allow-Credentials" not in headers
        _, headers = read_json(base_url + "/api/status", {
            "Origin": "null", "Cookie": "codex_ssh_bridge_token=test-token",
        })
        assert "Access-Control-Allow-Origin" not in headers
        try:
            post_json(base_url + "/api/agent-enabled", {"enabled": True}, {
                "Origin": "null", "Cookie": "codex_ssh_bridge_token=test-token",
            })
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("cross-origin cookies must not authorize panel mutations")
    finally:
        httpd.shutdown()
        httpd.server_close()
