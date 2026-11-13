import json
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error

import pytest

import server
from password_auth import AskpassBroker
from test_password_auth import entries, helper
from test_multi_sessions import post_json, start_manager_server


def wait_for(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def output(session):
    return "".join(item["text"] for item in session.snapshot(limit=6000)["events"] if item["type"] == "output")


def test_real_pty_edit_history_tab_interrupt_and_resize(tmp_path, monkeypatch):
    monkeypatch.setenv("PS1", "INTERACTIVE_TEST> ")
    monkeypatch.setenv("HISTFILE", "/dev/null")
    session = server.TerminalSession(["/bin/bash", "--noprofile", "--norc", "-i"])
    session.start()
    try:
        wait_for(lambda: "INTERACTIVE_TEST> " in output(session))
        session.write_terminal_input("printf 'EDIT:%s\\n' AB")
        session.write_terminal_input("\x1b[D\x7fZ\r")
        wait_for(lambda: "EDIT:ZB\r\n" in output(session))
        session.write_terminal_input("\x1b[A\r")
        wait_for(lambda: output(session).count("EDIT:ZB\r\n") == 2)
        target = tmp_path / "completion-file"
        target.touch()
        session.write_terminal_input(f"printf 'COMPLETE:%s\\n' {tmp_path}/completion-fi\t\r")
        wait_for(lambda: f"COMPLETE:{target}\r\n" in output(session))
        session.write_terminal_input("sleep 30\r")
        time.sleep(0.1)
        session.write_terminal_input("\x03")
        session.write_terminal_input("printf 'AFTER_INTERRUPT:%s\\n' ok\r")
        wait_for(lambda: "AFTER_INTERRUPT:ok\r\n" in output(session))
        session.resize(93, 27)
        session.write_terminal_input("stty size; printf 'TERM:%s\\n' \"$TERM\"\r")
        wait_for(lambda: "27 93\r\n" in output(session) and "TERM:xterm-256color\r\n" in output(session))
        session.write_terminal_input("printf '中文:%s\\n' 输入正常\r")
        wait_for(lambda: "中文:输入正常\r\n" in output(session))
        with pytest.raises(ValueError):
            session.resize(-1, 20)
    finally:
        session.stop()
        # Interactive bash can ignore SIGTERM. Reap this test-owned process
        # before its PTY descriptor can be reused by the next test's broker.
        if session.status()["running"]:
            try:
                os.killpg(session.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if session.reader_thread:
            session.reader_thread.join(3)
            assert not session.reader_thread.is_alive()


def test_auth_cards_never_route_stale_credentials_or_raw_keys_to_askpass(tmp_path, monkeypatch):
    manager = server.SessionManager()
    session = server.TerminalSession(["ssh", "gpu"], session_id="test-auth")
    manager.sessions[session.session_id] = session
    ready = threading.Event()
    broker = AskpassBroker(entries(), lambda kind, text: ready.set() if kind == "auth_prompt" else None, timeout=5)
    session.auth_broker = broker
    writes = []
    monkeypatch.setattr(session, "_write", writes.append)
    httpd, url = start_manager_server(tmp_path, manager)
    headers = {"Authorization": "Bearer test-token"}
    try:
        with ThreadPoolExecutor() as executor:
            answer = executor.submit(helper, broker, "(alice@192.0.2.6) Verification code: ")
            assert ready.wait(3)
            auth = broker.pending_info
            assert auth["kind"] == "code"
            for payload in [
                {"data": "x", "terminal": True},
                {"data": "private-test-code", "authRequestId": "stale-prompt"},
            ]:
                with pytest.raises(urllib.error.HTTPError):
                    post_json(url + "/api/input", {"sessionId": session.session_id, **payload}, headers)
            assert broker.pending and not writes
            post_json(url + "/api/input", {
                "sessionId": session.session_id, "authRequestId": auth["id"], "data": "654321", "private": True,
            }, headers)
            assert answer.result(5).stdout == "654321\n"
            with pytest.raises(urllib.error.HTTPError):
                post_json(url + "/api/input", {
                    "sessionId": session.session_id, "authRequestId": auth["id"], "data": "654321",
                }, headers)
            assert not writes
            post_json(url + "/api/input", {
                "sessionId": session.session_id, "data": "\x1b[A\t\x03", "terminal": True, "enter": True,
            }, headers)
            assert writes == ["\x1b[A\t\x03"]  # No implicit Enter in raw mode.
            assert "654321" not in json.dumps(session.status())
    finally:
        broker.close()
        httpd.shutdown()
        httpd.server_close()


def test_host_key_card_exposes_fingerprint_and_request_id_only():
    ready = threading.Event()
    broker = AskpassBroker(entries(), lambda kind, text: ready.set() if kind == "auth_prompt" else None, timeout=5)
    try:
        with ThreadPoolExecutor() as executor:
            result = executor.submit(helper, broker, "ED25519 fingerprint SHA256:test-only. Continue (yes/no)?", "confirm")
            assert ready.wait(3)
            info = broker.pending_info
            assert info["kind"] == "host_key" and "SHA256:test-only" in info["prompt"]
            broker.submit("yes", request_id=info["id"])
            assert result.result(5).stdout == "yes\n"
            assert broker.pending_info is None
    finally:
        broker.close()
