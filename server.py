#!/usr/bin/env python3
import argparse
import codecs
import hmac
import json
import os
import queue
import re
import secrets
import select
import shlex
import signal
import sys
import sysconfig
import threading
import time
import urllib.parse
import uuid
from collections import deque
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import profiles as profiles_module
import transfers as transfers_module
from profiles import (
    delete_profile_payload,
    get_profile,
    load_profiles,
    profile_to_command,
    redacted_profiles,
    save_profile_payload,
    set_default_profile_payload,
)
from platform_support import assert_supported_platform
from transfers import TransferManager
from state_paths import configure_state_home, state_file
from password_auth import AskpassBroker


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def static_dir():
    candidates = [
        ROOT / "static",
        Path(sysconfig.get_paths().get("data", sys.prefix)) / "codex-ssh-terminal-mcp" / "static",
    ]
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return candidates[0]


def refresh_state_files():
    profiles_module.PROFILE_FILE = state_file("connection-profiles.json", legacy_name=".connection-profiles.json")
    transfers_module.UPLOAD_STATE_FILE = state_file("upload-state.json", legacy_name=".upload-state.json")


def now_ms():
    return int(time.time() * 1000)


def clean_terminal_text(text):
    return ANSI_RE.sub("", text)


def events_to_lines(events, compact=True):
    lines = []
    buffer = ""
    pending_cr = False
    progress_updates = 0

    def append_line(text, event):
        clean = text.rstrip()
        if not clean:
            return
        lines.append(
            {
                "seq": event["seq"],
                "ts": event["ts"],
                "type": event["type"],
                "text": clean,
            }
        )

    for event in events:
        kind = event["type"]
        text = event.get("text", "")
        if kind != "output":
            label = "Codex" if kind == "agent_command" else kind
            for line in clean_terminal_text(text).splitlines() or [text]:
                append_line(f"[{label}] {line}", event)
            continue

        for ch in clean_terminal_text(text):
            if pending_cr:
                if ch == "\n":
                    append_line(buffer, event)
                    buffer = ""
                    pending_cr = False
                    continue
                if compact and buffer.strip():
                    progress_updates += 1
                if compact:
                    buffer = ""
                pending_cr = False

            if ch == "\r":
                pending_cr = True
            elif ch == "\n":
                append_line(buffer, event)
                buffer = ""
            elif ch == "\b":
                buffer = buffer[:-1]
            elif ch == "\t":
                buffer += "    "
            elif ch == "\x00":
                continue
            else:
                buffer += ch

    if buffer.strip():
        if pending_cr and compact:
            progress_updates += 1
        last_event = events[-1] if events else {"seq": 0, "ts": now_ms(), "type": "output"}
        append_line(buffer, last_event)

    return lines, progress_updates


class TerminalSession:
    def __init__(self, command=None, profile_name=None, session_id=None, cols=120, rows=32, history_limit=6000,
                 shared_code=None, control_socket=None):
        self.session_id = session_id or "sess_" + uuid.uuid4().hex[:10]
        self.command = command
        self.profile_name = profile_name
        self.cols = cols
        self.rows = rows
        self.history = deque(maxlen=history_limit)
        self.next_seq = 1
        self.subscribers = []
        self.lock = threading.RLock()
        self.pid = None
        self.returncode = None
        self.master_fd = None
        self.reader_thread = None
        self.started_at = None
        self.ended_at = None
        self.last_activity_at = None
        self.last_agent_command = ""
        self.agent_input_enabled = True
        self.auth_broker = None
        self.shared_code = shared_code
        self.control_path = control_socket
        self.authenticated = False
        self.auto_reconnect = bool(control_socket)
        self.recovery_state = "connecting" if control_socket else "disabled"
        self.recovery_message = ""

    def set_command(self, command, profile_name=None):
        with self.lock:
            if self._is_running_locked():
                raise RuntimeError("SSH session is already running")
            self.command = command
            self.profile_name = profile_name

    def start(self, command=None, profile_name=None):
        with self.lock:
            if self._is_running_locked():
                return
            if command is not None:
                self.command = command
                self.profile_name = profile_name
            if not self.command:
                raise RuntimeError("No SSH command configured. Create/select a connection profile first.")
            assert_supported_platform()
            import pty

            self.ended_at = None
            self.returncode = None
            self.started_at = now_ms()
            if self.auth_broker:
                self.auth_broker.close()
            self.auth_broker = (
                AskpassBroker.for_profile(self.profile_name, self._publish)
                if Path(self.command[0]).name == "ssh" and "BatchMode=yes" not in self.command else None
            )
            if self.shared_code:
                entry = self.auth_broker.entries.get(self.profile_name) if self.auth_broker else None
                if not entry:
                    if self.auth_broker:
                        self.auth_broker.close()
                    raise ValueError("验证码联动需要对应的本机密码配置")
                self.shared_code.enroll(self.auth_broker, (entry["user"], entry["host"].lower()))
            child_env = self.auth_broker.environment() if self.auth_broker else dict(os.environ)
            child_env["TERM"] = "xterm-256color"
            try:
                pid, master_fd = pty.fork()
            except BaseException:
                if self.auth_broker:
                    self.auth_broker.close()
                raise
            if pid == 0:
                try:
                    self._set_winsize(0, self.cols, self.rows)
                    os.execvpe(self.command[0], self.command, child_env)
                except Exception as exc:
                    os.write(2, f"exec failed: {exc}\n".encode("utf-8", errors="replace"))
                    os._exit(127)
            self.pid = pid
            self.master_fd = master_fd
            self._set_winsize(master_fd, self.cols, self.rows)
            label = f"profile '{self.profile_name}'" if self.profile_name else "SSH session"
            self._publish("system", f"Started {label}.\r\n")
            self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.reader_thread.start()

    def stop(self):
        with self.lock:
            # A user's Disconnect is authoritative, including during recovery.
            self.auto_reconnect = False
            self.recovery_state = "stopped"
            if self.auth_broker:
                self.auth_broker.close()
            if not self._is_running_locked():
                return
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.ended_at = now_ms()
            self._publish("system", "\r\nSession stopped.\r\n")

    def status(self, redact=False):
        with self.lock:
            running = self._is_running_locked()
            if redact:
                command = ["ssh", "[redacted]"] if self.command else []
            else:
                command = self.command or []
            return {
                "sessionId": self.session_id,
                "running": running,
                "command": command,
                "profileName": self.profile_name,
                "startedAt": self.started_at,
                "endedAt": self.ended_at,
                "lastActivityAt": self.last_activity_at,
                "lastAgentCommand": self.last_agent_command,
                "agentInputEnabled": self.agent_input_enabled,
                "authPromptPending": bool(self.auth_broker and self.auth_broker.pending),
                "authPrompt": self.auth_broker.pending_info if self.auth_broker else None,
                "connected": running and self.authenticated,
                "managedConnection": bool(self.control_path),
                "autoReconnect": self.auto_reconnect,
                "recoveryState": self.recovery_state,
                "recoveryMessage": self.recovery_message,
                "cols": self.cols,
                "rows": self.rows,
                "passwordAutofillEnabled": bool(self.auth_broker and self.auth_broker.autofill_enabled),
                "nextSeq": self.next_seq,
                "oldestSeq": self.history[0]["seq"] if self.history else self.next_seq,
                "historyCount": len(self.history),
            }

    def set_agent_input_enabled(self, enabled):
        with self.lock:
            self.agent_input_enabled = bool(enabled)
            state = "enabled" if self.agent_input_enabled else "paused"
            self._publish("system", f"\r\nCodex input {state} by user.\r\n")

    def write_user_input(self, data):
        if self.auth_broker and self.auth_broker.submit(data):
            return
        self._write(data)

    def write_terminal_input(self, data):
        # Raw keystrokes must never become partial askpass answers.
        with self.lock:
            if self.auth_broker:
                with self.auth_broker.lock:
                    if self.auth_broker.pending:
                        raise PermissionError("SSH 正在等待认证，请使用认证提示")
                    self._write(data)
            else:
                self._write(data)

    def write_auth_input(self, data, request_id):
        # A delayed click/Enter must not send a credential to the remote shell.
        if not request_id or not self.auth_broker:
            raise ValueError("没有待处理的认证提示")
        if not self.auth_broker.submit(data, request_id=request_id):
            raise ValueError("认证提示已结束")

    @staticmethod
    def _set_winsize(fd, cols, rows):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, cols, rows):
        if type(cols) is not int or type(rows) is not int or not (2 <= cols <= 500 and 1 <= rows <= 200):
            raise ValueError("终端尺寸超出范围")
        with self.lock:
            if not self._is_running_locked() or self.master_fd is None:
                raise RuntimeError("SSH session is not running")
            self._set_winsize(self.master_fd, cols, rows)
            self.cols, self.rows = cols, rows

    def write_agent_command(self, command):
        with self.lock:
            if self.auth_broker and self.auth_broker.pending:
                raise PermissionError("SSH is waiting for private authentication input in the web page")
            if not self.agent_input_enabled:
                raise PermissionError("Codex input is paused by the page")
            display_command = self._summarize_agent_command(command)
            self.last_agent_command = display_command
            self._publish("agent_command", display_command)
        line = command if command.endswith("\n") else command + "\n"
        self._write(line)

    def send_control(self, name):
        controls = {
            "ctrl-c": "\x03",
            "ctrl-d": "\x04",
            "enter": "\r",
        }
        if name not in controls:
            raise ValueError(f"Unknown control: {name}")
        if self.auth_broker and self.auth_broker.pending:
            if name == "enter":
                self.auth_broker.submit("")
            else:
                self.auth_broker.cancel()
            return
        self._write(controls[name])

    def snapshot(self, limit=200, response_format="events", line_limit=None, compact=True):
        with self.lock:
            items = list(self.history)[-limit:]
            return self._format_response(
                items,
                event_count=len(self.history),
                next_seq=self.next_seq,
                oldest_seq=self.history[0]["seq"] if self.history else self.next_seq,
                response_format=response_format,
                line_limit=line_limit if line_limit is not None else limit,
                compact=compact,
            )

    def events_after(self, after_seq=0, limit=200, response_format="events", line_limit=None, compact=True):
        with self.lock:
            all_events = [item for item in self.history if item["seq"] > after_seq]
            events = all_events[-limit:] if limit > 0 else all_events
            return self._format_response(
                events,
                event_count=len(all_events),
                next_seq=self.next_seq,
                oldest_seq=self.history[0]["seq"] if self.history else self.next_seq,
                response_format=response_format,
                line_limit=line_limit if line_limit is not None else limit,
                compact=compact,
            )

    def clear_history(self):
        with self.lock:
            self.history.clear()
        self._publish("system", "\r\nTerminal history cleared.\r\n")

    def _write(self, data):
        with self.lock:
            if not self._is_running_locked() or self.master_fd is None:
                raise RuntimeError("SSH session is not running")
            pending = memoryview(data.encode("utf-8", errors="replace"))
            while pending:
                pending = pending[os.write(self.master_fd, pending):]

    def _read_loop(self):
        broker = self.auth_broker
        try:
            self._read_pty()
        finally:
            if broker:
                broker.close()
            with self.lock:
                if self.master_fd is not None:
                    try:
                        os.close(self.master_fd)
                    except OSError:
                        pass
                    self.master_fd = None

    def _read_pty(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            with self.lock:
                fd = self.master_fd
                returncode = self._poll_locked()
            if fd is None:
                return
            if returncode is not None:
                self.ended_at = now_ms()
                self._publish("system", f"\r\nProcess exited with code {returncode}.\r\n")
                return
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(fd, 8192)
            except OSError as exc:
                self.ended_at = now_ms()
                self._publish("system", f"\r\nPTY closed: {exc}\r\n")
                return
            if not data:
                continue
            text = decoder.decode(data)
            self._publish("output", text)

    def _is_running_locked(self):
        return self._poll_locked() is None and self.pid is not None

    def _poll_locked(self):
        if self.pid is None:
            return self.returncode
        if self.returncode is not None:
            return self.returncode
        try:
            waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.returncode = 0
            return self.returncode
        if waited_pid == 0:
            return None
        if os.WIFEXITED(status):
            self.returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.returncode = 128 + os.WTERMSIG(status)
        else:
            self.returncode = status
        self.ended_at = now_ms()
        return self.returncode

    def _publish(self, kind, text):
        dead = []
        with self.lock:
            ts = now_ms()
            self.last_activity_at = ts
            event = {"seq": self.next_seq, "ts": ts, "type": kind, "text": text}
            self.next_seq += 1
            self.history.append(event)
            for sub in self.subscribers:
                try:
                    sub.put_nowait(event)
                except Exception:
                    dead.append(sub)
            for sub in dead:
                if sub in self.subscribers:
                    self.subscribers.remove(sub)

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def _summarize_agent_command(self, command, max_chars=800):
        if len(command) <= max_chars:
            return command
        head_len = 520
        tail_len = 180
        omitted = len(command) - head_len - tail_len
        return (
            command[:head_len]
            + f"\n...[agent command truncated, {omitted} chars omitted]...\n"
            + command[-tail_len:]
        )

    def _format_response(
        self,
        events,
        *,
        event_count,
        next_seq,
        oldest_seq,
        response_format,
        line_limit,
        compact,
    ):
        response = {
            "format": response_format,
            "nextSeq": next_seq,
            "oldestSeq": oldest_seq,
            "eventCount": event_count,
            "returnedEventCount": len(events),
            "omittedEventCount": max(0, event_count - len(events)),
        }
        if response_format == "lines":
            lines, progress_updates = events_to_lines(events, compact=compact)
            returned_lines = lines[-line_limit:] if line_limit > 0 else lines
            response.update(
                {
                    "lines": returned_lines,
                    "lineCount": len(lines),
                    "returnedLineCount": len(returned_lines),
                    "omittedLineCount": max(0, len(lines) - len(returned_lines)),
                    "progressUpdateCount": progress_updates,
                    "compact": compact,
                }
            )
        else:
            response["events"] = events
        return response


class SessionManager:
    def __init__(self, initial_command=None, initial_profile_name=None, max_sessions=4, maintain_connections=False):
        self.max_sessions = max(1, int(max_sessions))
        self.sessions = {}
        self.last_session_id = None
        self.lock = threading.RLock()
        self.connection_batch = None
        self.recovery = None
        if maintain_connections:
            from connection_recovery import ConnectionRecovery
            self.recovery = ConnectionRecovery(self)
            self.recovery.start()
        if initial_command:
            self.start_session(initial_command, initial_profile_name)

    def start_session(self, command=None, profile_name=None, shared_code=None, control_socket=None):
        if command is None:
            profile = get_profile(profile_name)
            if not profile:
                raise ValueError("Create/select a connection profile before starting SSH.")
            command = profile_to_command(profile)
            profile_name = profile.get("name")
        with self.lock:
            self._enforce_capacity_locked()
            session = TerminalSession(command, profile_name=profile_name,
                                      shared_code=shared_code, control_socket=control_socket)
            self.sessions[session.session_id] = session
            self.last_session_id = session.session_id
        try:
            session.start()
        except Exception:
            with self.lock:
                self.sessions.pop(session.session_id, None)
                if self.last_session_id == session.session_id:
                    self.last_session_id = next(reversed(self.sessions), None) if self.sessions else None
            raise
        return self.enriched_status(session)

    def start_profile(self, profile_name=None):
        profile = get_profile(profile_name)
        if not profile:
            raise ValueError("Create/select a connection profile before starting SSH.")
        if profile.get("mode") == "command":
            return self.start_session(profile_to_command(profile), profile_name=profile.get("name"))
        from connection_batch import managed_command, HF_PROFILES
        name = profile.get("name")
        jump_path = None
        if name in HF_PROFILES[1:]:
            with self.lock:
                jump = next((s for s in reversed(list(self.sessions.values()))
                             if s.profile_name == HF_PROFILES[0] and s.control_path and s.authenticated), None)
            jump_path = jump.control_path if jump else None
        command, path = managed_command(name, profile, jump_path=jump_path)
        with self.lock:
            existing = next((s for s in self.sessions.values() if s.profile_name == name
                             and s.control_path == path and s.status(redact=True)["running"]), None)
        if existing:
            return self.enriched_status(existing)
        return self.start_session(command, name, control_socket=path)

    def restore_terminal(self, session, command, authenticated=True):
        # Keep sessionId/history stable so open panels can resume the stream.
        # Hold both locks through start: Disconnect cannot race this restart.
        with self.lock, session.lock:
            if not session.auto_reconnect or session._is_running_locked():
                return False
            if session.reader_thread and session.reader_thread.is_alive():
                return False
            self._enforce_capacity_locked()
            original = session.command
            # The completed batch's OTP pool is closed and must never be
            # enrolled/reused while restoring a mux-only terminal channel.
            session.shared_code = None
            try:
                session.start(command, session.profile_name)
            finally:
                session.command = original
            session.authenticated = authenticated
            session.recovery_state = "ready" if authenticated else "connecting"
            message = "已恢复终端连接。这是新 shell，原命令不会自动重跑。" if authenticated else "正在重新登录；不会自动重跑原命令。"
            session._publish("system", "\r\n" + message + "\r\n")
            return True

    def start_batch(self):
        from connection_batch import BATCH_PROFILES, ConnectionBatch
        with self.lock:
            if self.connection_batch and self.connection_batch.phase == "connecting":
                return {"ok": True, "connectionBatch": self.connection_batch.status()}
            running = [s for s in self.sessions.values() if s.status(redact=True)["running"]]
            reusable = {s.profile_name for s in running if s.control_path}
            if len(running) + len(set(BATCH_PROFILES) - reusable) > self.max_sessions:
                raise ValueError("可用会话数量不足，请先断开不需要的单独连接")
            batch = ConnectionBatch(self)
            self.connection_batch = batch
            try:
                batch.start()
            except Exception:
                batch.cancel()
                self.connection_batch = None
                raise
        return {"ok": True, "connectionBatch": batch.status()}

    def submit_batch_code(self, batch_id, code):
        batch = self.connection_batch
        if not batch or batch.id != batch_id or batch.phase != "connecting":
            raise ValueError("本次四机连接已经结束，请重新连接")
        batch.code.submit(code)

    def stop_session(self, session_id=None):
        session = self.resolve(session_id)
        session.stop()
        return self.enriched_status(session)

    def stop_all(self):
        if self.connection_batch:
            self.connection_batch.cancel()
        stopped = []
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.stop()
            stopped.append(self.enriched_status(session))
        return stopped

    def resolve(self, session_id=None):
        with self.lock:
            if session_id:
                session = self.sessions.get(str(session_id))
                if not session:
                    raise ValueError(f"session not found: {session_id}")
                return session
            if len(self.sessions) == 1:
                return next(iter(self.sessions.values()))
            if not self.sessions:
                raise ValueError("no SSH session is available")
            raise ValueError("sessionId is required when multiple SSH sessions exist")

    def status(self, redact=False):
        with self.lock:
            sessions = [self.enriched_status(session, redact=redact) for session in self.sessions.values()]
        running = any(item["running"] for item in sessions)
        response = {
            "running": running,
            "features": {"batchConnect": True, "interactiveTerminal": True,
                         "connectionRecovery": self.recovery is not None, "isolatedAgentCommands": True},
            "runtimeVersion": "0.8.0",
            "controlPersistSeconds": 43200,
            "maxSessions": self.max_sessions,
            "sessionCount": len(sessions),
            "sessions": sessions,
            "activeSessionId": sessions[0]["sessionId"] if len(sessions) == 1 else None,
            "connectionBatch": self.connection_batch.status() if self.connection_batch else None,
        }
        if len(sessions) == 1:
            response.update(sessions[0])
        return response

    def list_sessions(self, redact=True):
        return {
            "ok": True,
            "maxSessions": self.max_sessions,
            "sessionCount": len(self.sessions),
            "sessions": [self.enriched_status(session, redact=redact) for session in self.sessions.values()],
        }

    def enriched_status(self, session, redact=False):
        data = session.status(redact=redact)
        data.update(self._profile_public_info(data.get("profileName")))
        return data

    def _enforce_capacity_locked(self):
        running = [session for session in self.sessions.values() if session.status(redact=True)["running"]]
        if len(running) >= self.max_sessions:
            raise ValueError(f"maximum running SSH sessions reached: {self.max_sessions}")

    def _profile_public_info(self, profile_name):
        if not profile_name:
            return {"mode": None, "hostHint": "", "userHint": "", "portHint": None, "proxyJumpHint": "", "description": ""}
        profiles = redacted_profiles()
        for profile in profiles.get("profiles", []):
            if profile.get("name") == profile_name:
                public = profile.get("public") or {}
                return {
                    "mode": profile.get("mode", "ssh"),
                    "hostHint": public.get("hostHint", ""),
                    "userHint": public.get("userHint", ""),
                    "portHint": public.get("portHint"),
                    "proxyJumpHint": public.get("proxyJumpHint", ""),
                    "commandHint": public.get("commandHint", ""),
                    "description": profile.get("description", ""),
                }
        return {"mode": None, "hostHint": "", "userHint": "", "portHint": None, "proxyJumpHint": "", "description": ""}


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "CodexSshBridge/0.1"
    QUIET_LOG_PATHS = {"/api/status", "/api/uploads", "/events"}

    def log_message(self, fmt, *args):
        # Keep passwords out of logs by avoiding request-body logging entirely.
        path = urllib.parse.urlparse(getattr(self, "path", "")).path
        if path in self.QUIET_LOG_PATHS or path == "/api/upload":
            return
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    @property
    def sessions(self):
        return self.server.sessions

    @property
    def transfers(self):
        return self.server.transfers

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._is_allowed_host():
            self._send_bad_host_error()
        elif path == "/":
            self._send_file(static_dir() / "index.html", "text/html; charset=utf-8", set_auth_cookie=True)
        elif path.startswith("/api/") and not self._is_authorized():
            self._send_auth_error()
        elif path == "/events" and not self._is_authorized():
            self._send_auth_error()
        elif path == "/events" and not self._is_allowed_browser_request():
            self._send_origin_error()
        elif path == "/api/status":
            params = urllib.parse.parse_qs(parsed.query)
            redact = params.get("redact", ["0"])[0] not in ("0", "false", "False", "no")
            self._send_json(self.sessions.status(redact=redact))
        elif path == "/api/sessions":
            self._send_json(self.sessions.list_sessions(redact=True))
        elif path == "/api/profiles":
            params = urllib.parse.parse_qs(parsed.query)
            redact = params.get("redact", ["0"])[0] not in ("0", "false", "False", "no")
            self._send_json(redacted_profiles() if redact else load_profiles())
        elif path == "/api/uploads":
            self._send_json({"ok": True, "uploads": self.transfers.list_uploads()})
        elif path == "/api/upload":
            params = urllib.parse.parse_qs(parsed.query)
            upload_id = params.get("id", [""])[0]
            self._send_json({"ok": True, "upload": self.transfers.get_upload(upload_id)})
        elif path == "/api/snapshot":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                session = self.sessions.resolve(params.get("sessionId", [""])[0] or None)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            limit = int(params.get("limit", ["200"])[0])
            response_format = params.get("format", ["events"])[0]
            line_limit = int(params.get("lineLimit", [str(limit)])[0])
            compact = params.get("compact", ["1"])[0] not in ("0", "false", "False", "no")
            self._send_json(
                session.snapshot(
                    limit=limit,
                    response_format=response_format,
                    line_limit=line_limit,
                    compact=compact,
                )
            )
        elif path == "/api/events":
            params = urllib.parse.parse_qs(parsed.query)
            try:
                session = self.sessions.resolve(params.get("sessionId", [""])[0] or None)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            after = int(params.get("after", ["0"])[0])
            limit = int(params.get("limit", ["200"])[0])
            response_format = params.get("format", ["events"])[0]
            line_limit = int(params.get("lineLimit", [str(limit)])[0])
            compact = params.get("compact", ["1"])[0] not in ("0", "false", "False", "no")
            self._send_json(
                session.events_after(
                    after_seq=after,
                    limit=limit,
                    response_format=response_format,
                    line_limit=line_limit,
                    compact=compact,
                )
            )
        elif path == "/events":
            self._send_events()
        else:
            static_root = static_dir()
            target = (static_root / path.lstrip("/")).resolve()
            if str(target).startswith(str(static_root.resolve())) and target.exists():
                self._send_file(target, self._mime(target))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if not self._is_allowed_host():
                self._send_bad_host_error()
                return
            if parsed.path.startswith("/api/"):
                if not self._is_authorized():
                    self._send_auth_error()
                    return
                if not self._is_allowed_browser_request():
                    self._send_origin_error()
                    return
            data = self._read_json()
            if parsed.path == "/api/start":
                if data.get("connectAll") is True:
                    self._send_json(self.sessions.start_batch())
                    return
                profile_name = str(data.get("profileName", "")).strip() or None
                self._send_json(self.sessions.start_profile(profile_name))
            elif parsed.path == "/api/stop":
                session_id = str(data.get("sessionId", "")).strip() or None
                if bool(data.get("all", False)):
                    self._send_json({"ok": True, "sessions": self.sessions.stop_all()})
                else:
                    self._send_json(self.sessions.stop_session(session_id))
            elif parsed.path == "/api/profiles":
                profiles, profile = save_profile_payload(
                    data.get("name"),
                    ssh_host=data.get("sshHost"),
                    ssh_port=data.get("sshPort", 22),
                    ssh_user=data.get("sshUser"),
                    command=data.get("command"),
                    description=data.get("description", ""),
                    proxy_jump=data.get("proxyJump"),
                    make_default=bool(data.get("makeDefault", False)),
                    storage_mode=data.get("storageMode", "keychain"),
                )
                self._send_json({"ok": True, "defaultProfile": profiles.get("defaultProfile"), "profile": profile})
            elif parsed.path == "/api/delete-profile":
                profiles, existed = delete_profile_payload(data.get("name"))
                self._send_json({"ok": True, "deleted": existed, "defaultProfile": profiles.get("defaultProfile")})
            elif parsed.path == "/api/default-profile":
                profiles = set_default_profile_payload(data.get("name"))
                self._send_json({"ok": True, "defaultProfile": profiles.get("defaultProfile")})
            elif parsed.path == "/api/uploads":
                upload = self.transfers.create_upload(
                    data.get("localPath"),
                    data.get("profileName"),
                    data.get("remotePath"),
                    if_exists=data.get("ifExists", "fail"),
                    method=data.get("method", "scp"),
                    checksum=data.get("checksum", "none"),
                )
                self._send_json({"ok": True, "upload": upload})
            elif parsed.path == "/api/upload-confirm":
                self._send_json({"ok": True, "upload": self.transfers.confirm_upload(data.get("id"))})
            elif parsed.path == "/api/upload-reject":
                self._send_json({"ok": True, "upload": self.transfers.reject_upload(data.get("id"))})
            elif parsed.path == "/api/upload-cancel":
                self._send_json({"ok": True, "upload": self.transfers.cancel_upload(data.get("id"))})
            elif parsed.path == "/api/upload-input":
                self._send_json(self.transfers.write_input(data.get("id"), data.get("data", ""), bool(data.get("enter", False))))
            elif parsed.path == "/api/input":
                if data.get("authBatchId"):
                    if data.get("private") is not True:
                        raise ValueError("批量验证码必须通过私密输入提交")
                    self.sessions.submit_batch_code(str(data["authBatchId"]), data.get("data", ""))
                    self._send_json({"ok": True})
                    return
                session = self.sessions.resolve(str(data.get("sessionId", "")).strip() or None)
                text = str(data.get("data", ""))
                if data.get("authRequestId"):
                    session.write_auth_input(text, str(data["authRequestId"]))
                elif data.get("terminal") is True:
                    session.write_terminal_input(text)
                else:
                    if data.get("enter", False):
                        text += "\r"
                    session.write_user_input(text)
                self._send_json({"ok": True})
            elif parsed.path == "/api/resize":
                session = self.sessions.resolve(str(data.get("sessionId", "")).strip() or None)
                session.resize(data.get("cols"), data.get("rows"))
                self._send_json({"ok": True})
            elif parsed.path == "/api/agent-command":
                session = self.sessions.resolve(str(data.get("sessionId", "")).strip() or None)
                command = str(data.get("command", "")).strip()
                if not command:
                    raise ValueError("command is required")
                expected_profile = str(data.get("expectedProfileName", "")).strip()
                if expected_profile and session.profile_name != expected_profile:
                    raise ValueError(
                        f"session/profile mismatch: {session.session_id} is {session.profile_name}, expected {expected_profile}"
                    )
                raise PermissionError("共享终端禁止 Codex 命令写入，请使用新版 send_command 独立命令通道。")
            elif parsed.path == "/api/control":
                session = self.sessions.resolve(str(data.get("sessionId", "")).strip() or None)
                session.send_control(str(data.get("name", "")))
                self._send_json({"ok": True, "sessionId": session.session_id})
            elif parsed.path == "/api/clear-history":
                session = self.sessions.resolve(str(data.get("sessionId", "")).strip() or None)
                session.clear_history()
                self._send_json({"ok": True, **self.sessions.enriched_status(session)})
            elif parsed.path == "/api/agent-enabled":
                session = self.sessions.resolve(str(data.get("sessionId", "")).strip() or None)
                session.set_agent_input_enabled(bool(data.get("enabled", True)))
                self._send_json(self.sessions.enriched_status(session))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, data, status=HTTPStatus.OK):
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type, set_auth_cookie=False):
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_auth_cookie:
            self._send_auth_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _send_events(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        try:
            session = self.sessions.resolve(params.get("sessionId", [""])[0] or None)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = session.subscribe()
        try:
            history_limit = int(params.get("history", ["200"])[0])
            for item in session.snapshot(limit=history_limit)["events"]:
                self._write_sse(item)
            while True:
                try:
                    item = q.get(timeout=15)
                    self._write_sse(item)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            session.unsubscribe(q)

    def _write_sse(self, item):
        self.wfile.write(b"event: terminal\n")
        self.wfile.write(b"data: ")
        self.wfile.write(json_bytes(item))
        self.wfile.write(b"\n\n")
        self.wfile.flush()

    def _mime(self, path):
        if path.suffix == ".css":
            return "text/css; charset=utf-8"
        if path.suffix == ".js":
            return "application/javascript; charset=utf-8"
        return "application/octet-stream"

    def _is_authorized(self):
        return self._authorization_source() is not None

    def _authorization_source(self):
        expected = self.server.api_token
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth.removeprefix("Bearer ").strip()
            if hmac.compare_digest(provided, expected):
                return "bearer"

        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_header)
            except Exception:
                cookie = SimpleCookie()
            morsel = cookie.get("codex_ssh_bridge_token")
            if morsel and hmac.compare_digest(morsel.value, expected):
                return "cookie"
        return None

    def _is_allowed_host(self):
        host = self.headers.get("Host", "")
        return self._is_allowed_netloc(host)

    def _is_allowed_browser_request(self):
        if self._authorization_source() == "bearer":
            return True
        return self._has_allowed_origin_or_referer()

    def _has_allowed_origin_or_referer(self):
        origin = self.headers.get("Origin", "")
        if origin:
            return self._is_allowed_url_origin(origin)
        referer = self.headers.get("Referer", "")
        if referer:
            return self._is_allowed_url_origin(referer)
        return False

    def _is_allowed_url_origin(self, value):
        try:
            parsed = urllib.parse.urlparse(value)
        except Exception:
            return False
        if parsed.scheme != "http":
            return False
        return self._is_allowed_netloc(parsed.netloc)

    def _is_allowed_netloc(self, value):
        hostname, port = self._host_and_port_from_netloc(value)
        return hostname in ("127.0.0.1", "localhost", "::1") and port == self.server.server_port

    def _host_and_port_from_netloc(self, value):
        value = (value or "").strip()
        if not value:
            return "", None
        if value.startswith("["):
            hostname, _, rest = value[1:].partition("]")
            port = rest[1:] if rest.startswith(":") else ""
            return hostname.lower(), self._parse_port(port)
        if value == "::1":
            return "::1", None
        if value.startswith("::1:"):
            return "::1", self._parse_port(value.rsplit(":", 1)[1])
        if value.count(":") == 1:
            hostname, port = value.rsplit(":", 1)
            return hostname.lower(), self._parse_port(port)
        return value.lower(), None

    def _parse_port(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _send_auth_cookie(self):
        self.send_header(
            "Set-Cookie",
            f"codex_ssh_bridge_token={self.server.api_token}; Path=/; HttpOnly; SameSite=Strict",
        )

    def _send_auth_error(self):
        self._send_json({"ok": False, "error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)

    def _send_bad_host_error(self):
        self._send_json({"ok": False, "error": "Bad Host"}, HTTPStatus.BAD_REQUEST)

    def _send_origin_error(self):
        self._send_json({"ok": False, "error": "Forbidden origin"}, HTTPStatus.FORBIDDEN)


class LocalOnlyHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, sessions, transfers, api_token):
        super().__init__(server_address, handler_class)
        if isinstance(sessions, TerminalSession):
            manager = SessionManager()
            manager.sessions[sessions.session_id] = sessions
            manager.last_session_id = sessions.session_id
            sessions = manager
        self.sessions = sessions
        self.transfers = transfers
        self.api_token = api_token

    def verify_request(self, request, client_address):
        ip = client_address[0]
        return ip == "127.0.0.1" or ip == "::1"


def build_command(args):
    if args.profile_name:
        profile = get_profile(args.profile_name)
        if not profile:
            raise SystemExit(f"Profile not found: {args.profile_name}")
        return profile_to_command(profile), profile.get("name")
    if args.command:
        return shlex.split(args.command), None
    if not args.ssh_host:
        profile = get_profile()
        if profile:
            return profile_to_command(profile), profile.get("name")
        return None, None
    target = f"{args.ssh_user}@{args.ssh_host}" if args.ssh_user else args.ssh_host
    return [
        "ssh",
        "-p",
        str(args.ssh_port),
        target,
    ], None


def main():
    assert_supported_platform()

    parser = argparse.ArgumentParser(description="Local web terminal bridge for Codex-assisted SSH.")
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address. Keep 127.0.0.1 for safety.")
    parser.add_argument("--port", type=int, default=18766, help="Local web port.")
    parser.add_argument("--ssh-host", help="SSH host.")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH port.")
    parser.add_argument("--ssh-user", help="SSH user. If omitted, ssh uses the local username.")
    parser.add_argument("--command", help="Override the SSH command, for advanced use.")
    parser.add_argument("--profile-name", help="Saved connection profile to use.")
    parser.add_argument("--no-auto-start", action="store_true", help="Do not start SSH until the page calls Start.")
    parser.add_argument("--upload-retention-days", type=int, default=14, help="Days to retain finished upload tasks.")
    parser.add_argument("--upload-pending-ttl-days", type=int, default=7, help="Days to retain unconfirmed upload requests before expiring them.")
    parser.add_argument("--upload-log-limit", type=int, default=120, help="Maximum compact log entries retained per upload.")
    parser.add_argument("--upload-log-entry-max-chars", type=int, default=8000, help="Maximum characters retained per upload log entry.")
    parser.add_argument("--upload-max-records", type=int, default=500, help="Maximum upload records retained after pruning finished tasks.")
    parser.add_argument("--max-sessions", type=int, default=4, help="Maximum concurrently running SSH sessions.")
    parser.add_argument("--state-dir", help="Override local state/config directory for profiles and upload state.")
    parser.add_argument(
        "--api-token",
        default=os.environ.get("CODEX_SSH_TERMINAL_TOKEN") or secrets.token_urlsafe(32),
        help="Local API token. Defaults to CODEX_SSH_TERMINAL_TOKEN or a generated token.",
    )
    args = parser.parse_args()
    if args.state_dir:
        configure_state_home(args.state_dir)
        refresh_state_files()

    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing to bind non-local address. Use 127.0.0.1.")

    command, profile_name = build_command(args)
    sessions = SessionManager(max_sessions=args.max_sessions, maintain_connections=True)
    if not args.no_auto_start and command:
        if profile_name:
            sessions.start_profile(profile_name)
        else:
            sessions.start_session(command, profile_name)

    server = LocalOnlyHTTPServer(
        (args.bind, args.port),
        BridgeHandler,
        sessions,
        TransferManager(
            retention_days=args.upload_retention_days,
            pending_ttl_days=args.upload_pending_ttl_days,
            log_limit=args.upload_log_limit,
            log_entry_max_chars=args.upload_log_entry_max_chars,
            max_records=args.upload_max_records,
        ),
        args.api_token,
    )
    url = f"http://{args.bind}:{args.port}/"
    print(f"Codex SSH terminal bridge listening on {url}")
    print("Open this URL in your browser. Press Ctrl+C here to stop the bridge.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if sessions.recovery:
            sessions.recovery.close()
        sessions.stop_all()
        server.server_close()


if __name__ == "__main__":
    main()
