#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp_panel import PANEL_URI, PANEL_VERSION, bridge_origin, panel_tools, read_panel_resource, resource_descriptor
import profiles as profiles_module
import runtime_config as runtime_config_module
from profiles import (
    delete_profile_payload,
    load_profiles,
    redacted_profiles,
    set_default_profile_payload,
)
from platform_support import assert_supported_platform, platform_status
from runtime_config import (
    bridge_python_command,
    runtime_config_status,
    set_runtime_config_payload,
)
from state_paths import ENV_HOME, chmod_private_file, configure_state_home, state_dir, state_file


ROOT = Path(__file__).resolve().parent
STATE_FILE = state_file("mcp-state.json", legacy_name=".mcp-state.json")
PID_FILE = state_file("bridge.pid", legacy_name=".bridge.pid")
LOG_FILE = state_file("bridge.log", legacy_name=".bridge.log")
DEFAULT_BASE_URL = os.environ.get("CODEX_SSH_TERMINAL_URL", "http://127.0.0.1:18766")
ENV_TOKEN = "CODEX_SSH_TERMINAL_TOKEN"
BRIDGE_LOG_MAX_BYTES = int(os.environ.get("CODEX_SSH_TERMINAL_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
BRIDGE_LOG_BACKUPS = int(os.environ.get("CODEX_SSH_TERMINAL_LOG_BACKUPS", "3"))


def refresh_state_files():
    global STATE_FILE, PID_FILE, LOG_FILE
    STATE_FILE = state_file("mcp-state.json", legacy_name=".mcp-state.json")
    PID_FILE = state_file("bridge.pid", legacy_name=".bridge.pid")
    LOG_FILE = state_file("bridge.log", legacy_name=".bridge.log")
    profiles_module.PROFILE_FILE = state_file("connection-profiles.json", legacy_name=".connection-profiles.json")
    runtime_config_module.RUNTIME_CONFIG_FILE = state_file("runtime-config.json", legacy_name=".runtime-config.json")


def request_json(method, url, payload=None, timeout=10, token=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get(base_url, path, params=None, timeout=10, token=None):
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return request_json("GET", url, timeout=timeout, token=token)


def http_post(base_url, path, payload=None, timeout=10, token=None):
    return request_json("POST", base_url.rstrip("/") + path, payload or {}, timeout=timeout, token=token)


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    chmod_private_file(STATE_FILE)


def state_key(base_url):
    return base_url.rstrip("/")


def state_entry(state, base_url):
    key = state_key(base_url)
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = {}
        state[key] = entry
    return entry


def get_auth_token(base_url):
    if os.environ.get(ENV_TOKEN):
        return os.environ[ENV_TOKEN]
    return state_entry(load_state(), base_url).get("authToken")


def set_auth_token(base_url, token):
    state = load_state()
    state_entry(state, base_url)["authToken"] = token
    save_state(state)


def get_cursor(base_url, session_id=None):
    entry = state_entry(load_state(), base_url)
    if session_id:
        cursors = entry.get("sessionCursors") or {}
        return int(cursors.get(session_id, 0))
    return int(entry.get("lastSeq", 0))


def update_cursor(base_url, last_seq, session_id=None):
    state = load_state()
    entry = state_entry(state, base_url)
    if session_id:
        cursors = entry.get("sessionCursors")
        if not isinstance(cursors, dict):
            cursors = {}
            entry["sessionCursors"] = cursors
        cursors[session_id] = last_seq
    else:
        entry["lastSeq"] = last_seq
    save_state(state)


def rotate_log_file(path, max_bytes=BRIDGE_LOG_MAX_BYTES, backups=BRIDGE_LOG_BACKUPS):
    path = Path(path)
    max_bytes = max(0, int(max_bytes))
    backups = max(0, int(backups))
    if max_bytes <= 0 or backups <= 0 or not path.exists():
        return False
    try:
        if path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False

    for index in range(backups, 0, -1):
        rotated = path.with_name(f"{path.name}.{index}")
        if index == backups and rotated.exists():
            rotated.unlink()
        previous = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        if previous.exists():
            previous.rename(rotated)
            chmod_private_file(rotated)
    return True


def wait_for_bridge(base_url, timeout_seconds=8, token=None):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            return http_get(base_url, "/api/status", {"redact": "1"}, timeout=1, token=token)
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.25)
    raise RuntimeError(f"bridge did not become reachable: {last_error}")


class PanelKeyExpired(ValueError):
    """The private request was rejected before it could be dispatched."""


class TerminalTools:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self._panel_cipher = None
        self._panel_socket = None
        self._panel_clipboard = None
        self._agent_commands = None

    def agent_commands(self):
        if self._agent_commands is None:
            from agent_commands import AgentCommands
            self._agent_commands = AgentCommands()
        return self._agent_commands

    def panel_transport(self):
        if self._panel_socket is None:
            from panel_transport import PanelTransport
            self._panel_socket = PanelTransport(self)
        return self._panel_socket

    def _auth_token(self):
        return get_auth_token(self.base_url)

    def _get(self, path, params=None, timeout=10):
        return http_get(self.base_url, path, params, timeout=timeout, token=self._auth_token())

    def _post(self, path, payload=None, timeout=10):
        return http_post(self.base_url, path, payload, timeout=timeout, token=self._auth_token())

    def panel_bootstrap(self):
        origin = bridge_origin(self.base_url)
        started = self.start_config_server(port=urllib.parse.urlparse(origin).port)
        if started.get("status", {}).get("authenticated") is False:
            raise RuntimeError("The running bridge belongs to a different local configuration.")
        if self._panel_cipher is None:
            from panel_crypto import PanelCipher
            self._panel_cipher = PanelCipher()
        return {
            "content": [{"type": "text", "text": "SSH 终端已就绪。请在面板中选择服务器并连接。"}],
            "structuredContent": {"ready": True},
            "_meta": {"sshBridge": {
                "transport": "mcp", "encryption": self._panel_cipher.descriptor(),
                "panelVersion": PANEL_VERSION, "privateClipboard": True,
                "realtime": self.panel_transport().descriptor(),
            }},
        }

    def open_ssh_panel(self):
        return self.panel_bootstrap()

    def panel_request(self, method, path, payload=None):
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith("/api/"):
            raise ValueError("Invalid panel request.")
        allowed_get = {
            "/api/status", "/api/sessions", "/api/profiles", "/api/uploads",
            "/api/upload", "/api/snapshot", "/api/events",
        }
        allowed_post = {
            "/api/start", "/api/stop", "/api/profiles", "/api/delete-profile",
            "/api/default-profile", "/api/upload-confirm", "/api/upload-reject",
            "/api/upload-cancel", "/api/control", "/api/clear-history", "/api/agent-enabled", "/api/resize",
        }
        if method == "GET" and parsed.path in allowed_get:
            data = self._get(path, timeout=5)
            if parsed.path == "/api/status":
                maintenance = self.maintenance_status()
                if maintenance:
                    data = {**data, "maintenance": maintenance,
                            "features": {**data.get("features", {}), "connectionMaintenanceCompat": True}}
        elif method == "POST" and path in allowed_post:
            data = self._post(path, payload or {}, timeout=10)
        else:
            raise ValueError("This operation is not available through the panel data channel.")
        return {"content": [], "_meta": {"payload": data}}

    def maintenance_status(self):
        try:
            report = json.loads(state_file("maintenance-status.json").read_text())
            if not report.get("running") or report.get("expiresAt", 0) <= time.time() * 1000:
                return None
            os.kill(int(report["pid"]), 0)
            return {key: report[key] for key in ("version", "expiresAt", "message", "heldProfiles") if key in report}
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def panel_private_input(self, envelope):
        cipher = self._panel_cipher
        key_id = envelope.get("keyId") if isinstance(envelope, dict) else None
        if cipher is None or (isinstance(key_id, str) and key_id != cipher.key_id):
            raise PanelKeyExpired("SSH panel input key expired.")
        path, payload = cipher.decrypt(envelope)
        if path == "/panel/clipboard":
            from panel_crypto import clipboard_reply_key, encrypt_clipboard_reply
            operation = payload.get("operation")
            if operation not in ("read", "write"):
                raise ValueError("Invalid clipboard operation.")
            key = clipboard_reply_key(payload.get("responseKey")) if operation == "read" else None
            if operation == "write" and (not isinstance(payload.get("text"), str) or len(payload["text"].encode("utf-8")) > 1024 * 1024):
                raise ValueError("Clipboard text is invalid or too large.")
            if self._panel_clipboard is None:
                from panel_clipboard import LocalClipboard
                self._panel_clipboard = LocalClipboard()
            if operation == "read":
                data = {"encryptedClipboard": encrypt_clipboard_reply(self._panel_clipboard.read(), key)}
            else:
                self._panel_clipboard.write(payload["text"])
                data = {"ok": True}
        else:
            data = self._post(path, payload, timeout=10)
        return {"content": [], "_meta": {"payload": data}}

    def bridge_status(self):
        try:
            data = self._get("/api/status", {"redact": "1"}, timeout=2)
            return {"reachable": True, "baseUrl": self.base_url, **data}
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return {
                    "reachable": True,
                    "authenticated": False,
                    "baseUrl": self.base_url,
                    "error": "Unauthorized: local API token is missing or does not match this bridge.",
                    "hint": (
                        "If this bridge was started manually, open the browser URL to use the page directly, "
                        "or restart it through start_config_server/start_bridge so MCP can generate and store the token."
                    ),
                }
            return {
                "reachable": False,
                "baseUrl": self.base_url,
                "error": str(exc),
                "hint": "Call start_bridge, or run server.py and open the HTML page manually.",
            }
        except Exception as exc:
            return {
                "reachable": False,
                "baseUrl": self.base_url,
                "error": str(exc),
                "hint": "Call start_bridge, or run server.py and open the HTML page manually.",
            }

    def runtime_config_status(self):
        return {"ok": True, "platform": platform_status(), "stateDir": str(state_dir()), **runtime_config_status()}

    def set_runtime_config(self, mode, python_path=None, conda_env=None, conda_executable=None):
        data = set_runtime_config_payload(mode, python_path, conda_env, conda_executable)
        return {"ok": True, **data}

    def list_profiles(self):
        data = redacted_profiles()
        return {"ok": True, **data}

    def list_sessions(self):
        return self._get("/api/sessions", timeout=5)

    def delete_profile(self, name):
        data, existed = delete_profile_payload(name)
        return {"ok": True, "deleted": existed, "defaultProfile": data.get("defaultProfile")}

    def set_default_profile(self, name):
        data = set_default_profile_payload(name)
        return {"ok": True, "defaultProfile": data.get("defaultProfile")}

    def start_bridge(
        self,
        profile_name=None,
        port=18766,
        no_auto_start=False,
    ):
        selected_profile_name = None
        profiles = load_profiles()
        profile_name = profile_name or profiles.get("defaultProfile")
        if not profile_name:
            raise ValueError(
                "No connection profile is configured. Ask the user to open the HTML page, create a connection profile, and then choose a saved profile."
            )
        if profile_name not in profiles["profiles"]:
            raise ValueError(f"profile not found: {profile_name}")
        selected_profile_name = profile_name

        base_url = urllib.parse.urlunparse(("http", f"127.0.0.1:{port}", "", "", "", ""))
        original_base_url = self.base_url
        self.base_url = base_url
        status = self.bridge_status()
        self.base_url = original_base_url
        if status.get("reachable"):
            self.base_url = base_url
            if status.get("authenticated") is False:
                raise RuntimeError(status["error"] + " " + status["hint"])
            if no_auto_start:
                return {
                    "ok": True,
                    "alreadyRunning": bool(status.get("running")),
                    "reusedBridge": True,
                    "baseUrl": base_url,
                    "browserUrl": base_url + "/",
                    "profileName": selected_profile_name,
                    "status": status,
                    "message": "Bridge is reachable. no_auto_start=true, so no SSH session was started.",
                }
            try:
                started = self._post("/api/start", {"profileName": selected_profile_name}, timeout=5)
                return {
                    "ok": True,
                    "alreadyRunning": False,
                    "reusedBridge": True,
                    "baseUrl": base_url,
                    "browserUrl": base_url + "/",
                    "profileName": selected_profile_name,
                    "status": started,
                }
            except urllib.error.HTTPError:
                raise

        token = secrets.token_urlsafe(32)
        set_auth_token(base_url, token)
        args = self._server_command(port, ["--profile-name", selected_profile_name])
        if no_auto_start:
            args.append("--no-auto-start")

        proc = self._spawn_bridge(args, api_token=token)
        self.base_url = base_url
        data = wait_for_bridge(base_url, token=token)
        return {
            "ok": True,
            "alreadyRunning": False,
            "pid": proc.pid,
            "baseUrl": base_url,
            "logFile": str(LOG_FILE),
            "status": data,
            "browserUrl": base_url + "/",
            "profileName": selected_profile_name,
        }

    def start_config_server(self, port=18766):
        base_url = urllib.parse.urlunparse(("http", f"127.0.0.1:{port}", "", "", "", ""))
        original_base_url = self.base_url
        self.base_url = base_url
        status = self.bridge_status()
        self.base_url = original_base_url
        if status.get("reachable"):
            self.base_url = base_url
            return {
                "ok": True,
                "alreadyRunning": True,
                "baseUrl": base_url,
                "browserUrl": base_url + "/",
                "status": status,
                "message": "Open the browser URL and create or edit connection profiles in the HTML page.",
            }

        token = secrets.token_urlsafe(32)
        set_auth_token(base_url, token)
        args = self._server_command(port, ["--no-auto-start"])
        proc = self._spawn_bridge(args, api_token=token)
        self.base_url = base_url
        data = wait_for_bridge(base_url, token=token)
        return {
            "ok": True,
            "alreadyRunning": False,
            "pid": proc.pid,
            "baseUrl": base_url,
            "browserUrl": base_url + "/",
            "logFile": str(LOG_FILE),
            "status": data,
            "message": "Open the browser URL and create or edit connection profiles in the HTML page.",
        }

    def _server_command(self, port, extra_args=None):
        assert_supported_platform()
        args = bridge_python_command(require_config=True) + [
            str(ROOT / "server.py"),
            "--port",
            str(port),
        ]
        if extra_args:
            args.extend(extra_args)
        return args

    def _spawn_bridge(self, args, api_token=None):
        rotate_log_file(LOG_FILE)
        log = LOG_FILE.open("a", encoding="utf-8")
        chmod_private_file(LOG_FILE)
        env = os.environ.copy()
        if api_token:
            env[ENV_TOKEN] = api_token
        env[ENV_HOME] = str(state_dir())
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid) + "\n")
        chmod_private_file(PID_FILE)
        return proc

    def stop_bridge(self, stop_ssh=True, kill_process=True):
        raise PermissionError("共享 SSH 连接由用户管理。请在面板点击断开；Codex 只能取消自己的独立命令。")

    def send_command(self, command, session_id=None, expected_profile_name=None):
        if not command or not command.strip():
            raise ValueError("command is required")
        sessions = self.list_sessions().get("sessions", [])
        if session_id:
            session = next((s for s in sessions if s["sessionId"] == session_id), None)
        elif len(sessions) == 1:
            session = sessions[0]
        else:
            raise ValueError("session_id is required when multiple sessions exist")
        if not session:
            raise ValueError("session not found")
        return self.agent_commands().start(command, session, expected_profile_name)

    def command_status(self, command_id, limit=80, full=False, peek=False):
        return self.agent_commands().resolve(command_id).status(limit=limit, full=full, peek=peek)

    def cancel_command(self, command_id):
        return self.agent_commands().resolve(command_id).cancel()

    def snapshot(
        self,
        session_id=None,
        limit=80,
        event_limit=1000,
        full=False,
        peek=False,
        events=False,
        no_compact=False,
        after=None,
        channel="auto",
    ):
        if channel not in ("auto", "agent", "terminal"):
            raise ValueError("channel must be auto, agent, or terminal")
        if channel != "terminal" and (channel == "agent" or self._agent_commands is not None):
            try:
                job = self.agent_commands().resolve(session_id=session_id)
            except ValueError:
                if channel == "agent":
                    raise
            else:
                return job.status(after=after, full=full, peek=peek, limit=limit, events=events)
        params_session = {"sessionId": session_id} if session_id else {}
        if events:
            if full:
                data = self._get("/api/snapshot", {"limit": limit, **params_session})
            else:
                cursor = after if after is not None else get_cursor(self.base_url, session_id)
                data = self._get("/api/events", {"after": cursor, "limit": limit, **params_session})
                if not peek:
                    update_cursor(self.base_url, data.get("nextSeq", cursor + 1) - 1, session_id=session_id)
            return data

        cursor = 0 if full else after if after is not None else get_cursor(self.base_url, session_id)
        data = self._get(
            "/api/events",
            {
                "after": cursor,
                "limit": event_limit,
                "format": "lines",
                "lineLimit": limit,
                "compact": "0" if no_compact else "1",
                **params_session,
            },
        )
        if not full and not peek:
            update_cursor(self.base_url, data.get("nextSeq", cursor + 1) - 1, session_id=session_id)
        return data

    def reset_cursor(self, session_id=None):
        state = load_state()
        entry = state_entry(state, self.base_url)
        if session_id:
            cursors = entry.get("sessionCursors") or {}
            cursors.pop(session_id, None)
            entry["sessionCursors"] = cursors
        else:
            entry.pop("lastSeq", None)
            entry.pop("sessionCursors", None)
        save_state(state)
        return {"ok": True, "message": "MCP local cursor reset"}

    def clear_history(self, session_id=None):
        payload = {"sessionId": session_id} if session_id else {}
        data = self._post("/api/clear-history", payload)
        update_cursor(self.base_url, data.get("nextSeq", 1) - 1, session_id=session_id)
        return data

    def control(self, name, session_id=None, command_id=None):
        if name not in ("ctrl-c", "ctrl-d", "enter"):
            raise ValueError("name must be one of: ctrl-c, ctrl-d, enter")
        return self.agent_commands().resolve(command_id, session_id).control(name)

    def request_upload(self, local_path, profile_name, remote_path, if_exists="fail", method="scp", checksum="none"):
        payload = {
            "localPath": local_path,
            "profileName": profile_name,
            "remotePath": remote_path,
            "ifExists": if_exists,
            "method": method,
            "checksum": checksum,
        }
        data = self._post("/api/uploads", payload, timeout=10)
        data["message"] = "Upload request created. Ask the user to review and confirm it in the HTML page before transfer starts."
        return data

    def list_uploads(self):
        return self._get("/api/uploads", timeout=5)

    def upload_status(self, upload_id):
        return self._get("/api/upload", {"id": upload_id}, timeout=5)

    def cancel_upload(self, upload_id):
        return self._post("/api/upload-cancel", {"id": upload_id}, timeout=5)


def tool_schema():
    return panel_tools() + [
        {
            "name": "bridge_status",
            "description": "Check whether the local SSH terminal bridge is reachable and return its SSH/session status.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "runtime_config_status",
            "description": "Check the local Python or Conda runtime configuration used to start the bridge server.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "set_runtime_config",
            "description": "Persist the local Python or Conda runtime used to start server.py. This stores no SSH passwords or secrets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["python", "conda", "current"]},
                    "python_path": {"type": "string", "description": "Python executable path when mode=python."},
                    "conda_env": {"type": "string", "description": "Conda environment name when mode=conda."},
                    "conda_executable": {"type": "string", "description": "Conda executable path or name when mode=conda. Defaults to conda."},
                },
                "required": ["mode"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_profiles",
            "description": "List saved SSH connection profiles. Profiles never include passwords or verification codes.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_sessions",
            "description": "List active SSH sessions with compact, redacted server metadata. Use session_id from this list for precise multi-server commands and snapshots.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "delete_profile",
            "description": "Delete a saved SSH connection profile.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "set_default_profile",
            "description": "Set the default SSH connection profile used by start_bridge when no host, command, or profile is provided.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "start_bridge",
            "description": "Start the local Web/PTY SSH bridge and, unless no_auto_start=true, create a new SSH session for the requested profile. Locally configured passwords are filled by OpenSSH askpass; the user enters 2FA in the web page. Never request passwords or 2FA codes through MCP.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "profile_name": {"type": "string", "description": "Saved profile name. If omitted, the default profile is used."},
                    "port": {"type": "integer", "default": 18766},
                    "no_auto_start": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "start_config_server",
            "description": "Start only the local HTML configuration page, without requiring or starting an SSH profile.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "port": {"type": "integer", "default": 18766},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "stop_bridge",
            "description": "Shared SSH connections are user-owned. Disconnect using the panel; agents must only cancel their own command channels.",
            "_meta": {"ui": {"visibility": ["app"]}},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "stop_ssh": {"type": "boolean", "default": True},
                    "kill_process": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "send_command",
            "description": "Run a non-secret command in its own SSH channel, isolated from the user's terminal and other chats. Returns commandId; read output using command_status. Each command starts a fresh shell: include cd and environment activation explicitly. Reuses authenticated SSH only, never prompts for credentials or replays commands.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "session_id": {"type": "string", "description": "Target sessionId from list_sessions. Required by the bridge when multiple sessions exist."},
                    "expected_profile_name": {"type": "string", "description": "Optional guard; command is rejected if session_id belongs to another profile."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "command_status",
            "description": "Read output and exit status for an isolated command created by this chat. Prefer this after send_command; never read another chat's command channel.",
            "inputSchema": {
                "type": "object", "properties": {
                    "command_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 80},
                    "full": {"type": "boolean", "default": False},
                    "peek": {"type": "boolean", "default": False},
                }, "required": ["command_id"], "additionalProperties": False,
            },
        },
        {
            "name": "cancel_command",
            "description": "Interrupt only this chat's specified command channel. Never disconnects the SSH master or the user's terminal. Check remote task status afterwards.",
            "inputSchema": {
                "type": "object", "properties": {"command_id": {"type": "string"}},
                "required": ["command_id"], "additionalProperties": False,
            },
        },
        {
            "name": "snapshot",
            "description": "Read output. Auto reads this chat's latest isolated command for the session, otherwise the user's terminal. Set channel=terminal explicitly to inspect panel output; prefer command_status for commands.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Target sessionId from list_sessions. Required by the bridge when multiple sessions exist."},
                    "limit": {"type": "integer", "default": 80, "description": "Line limit by default, or event limit when events=true."},
                    "event_limit": {"type": "integer", "default": 1000, "description": "Maximum raw events to inspect when building compact lines."},
                    "full": {"type": "boolean", "default": False, "description": "Read recent history from the beginning of retained history instead of the saved MCP cursor."},
                    "peek": {"type": "boolean", "default": False, "description": "Read without advancing the saved MCP cursor."},
                    "events": {"type": "boolean", "default": False, "description": "Return raw event chunks instead of compact lines."},
                    "no_compact": {"type": "boolean", "default": False, "description": "Disable carriage-return progress compaction."},
                    "after": {"type": "integer", "description": "Explicit seq cursor to read after."},
                    "channel": {"type": "string", "enum": ["auto", "agent", "terminal"], "default": "auto"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "reset_cursor",
            "description": "Reset the MCP client's local incremental read cursor. Pass session_id to reset only one session cursor.",
            "inputSchema": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "clear_history",
            "description": "Clear one SSH session's in-memory terminal history and move that session's MCP cursor to the new end.",
            "inputSchema": {
                "type": "object",
                "properties": {"session_id": {"type": "string", "description": "Target sessionId from list_sessions. Required by the bridge when multiple sessions exist."}},
                "additionalProperties": False,
            },
        },
        {
            "name": "control",
            "description": "Send ctrl-c/ctrl-d/enter only to this chat's isolated command channel. Never writes to the user's panel terminal. Prefer command_id to select a command precisely.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": ["ctrl-c", "ctrl-d", "enter"]},
                    "session_id": {"type": "string", "description": "Target sessionId from list_sessions. Required by the bridge when multiple sessions exist."},
                    "command_id": {"type": "string", "description": "Isolated commandId returned by send_command."},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
        {
            "name": "request_upload",
            "description": "Create a local-file upload request for the user to confirm in the HTML page. File bytes are not sent through MCP or terminal history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "Local file path on the machine running this MCP server."},
                    "profile_name": {"type": "string", "description": "Saved host/port/user profile name. Optional ProxyJump profiles are supported for uploads; full SSH command profiles are not."},
                    "remote_path": {"type": "string", "description": "Destination path on the remote host."},
                    "if_exists": {"type": "string", "enum": ["fail", "overwrite", "backup"], "default": "fail"},
                    "method": {"type": "string", "enum": ["scp", "rsync"], "default": "scp"},
                    "checksum": {"type": "string", "enum": ["none", "sha256"], "default": "none", "description": "Remote size is always verified after upload. Use sha256 for an additional checksum check."},
                },
                "required": ["local_path", "profile_name", "remote_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_uploads",
            "description": "List upload requests and transfer statuses. Does not include file contents.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "upload_status",
            "description": "Get one upload request or transfer status by upload_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"upload_id": {"type": "string"}},
                "required": ["upload_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "cancel_upload",
            "description": "Cancel a running upload transfer.",
            "inputSchema": {
                "type": "object",
                "properties": {"upload_id": {"type": "string"}},
                "required": ["upload_id"],
                "additionalProperties": False,
            },
        },
    ]


class McpServer:
    def __init__(self, tools):
        self.tools = tools

    def serve(self):
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = self.handle(request)
            except Exception as exc:
                response = self.error(None, -32700, str(exc))
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()

    def handle(self, request):
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if request_id is None and method and method.startswith("notifications/"):
            return None
        if method == "initialize":
            return self.result(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "codex-ssh-terminal-mcp", "version": PANEL_VERSION},
                    "instructions": (
                        "SSH masters and panel terminals are user-owned and shared across chats. "
                        "Use send_command for an isolated per-command channel and command_status for output; "
                        "each command is a fresh shell, so include cd/environment activation explicitly. "
                        "Never stop_bridge or send exit/control input to panel terminals for cleanup. "
                        "Cancel only command IDs created by this chat. Never automatically replay failed commands. "
                        "When SSH authentication expires, ask the user to authenticate privately in the panel."
                    ),
                },
            )
        if method == "ping":
            return self.result(request_id, {})
        if method == "tools/list":
            return self.result(request_id, {"tools": tool_schema()})
        if method == "resources/list":
            return self.result(request_id, {"resources": [resource_descriptor()]})
        if method == "resources/templates/list":
            return self.result(request_id, {"resourceTemplates": []})
        if method == "resources/read":
            if params.get("uri") != PANEL_URI:
                return self.error(request_id, -32602, "Unknown UI resource")
            try:
                return self.result(request_id, {"contents": [read_panel_resource(self.tools.base_url)]})
            except Exception:
                return self.error(request_id, -32603, "Could not load the SSH panel assets.")
        if method == "tools/call":
            return self.call_tool(request_id, params)
        return self.error(request_id, -32601, f"Method not found: {method}")

    def call_tool(self, request_id, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            allowed_tools = {tool["name"] for tool in tool_schema()}
            if name not in allowed_tools or not hasattr(self.tools, name):
                raise ValueError(f"Unknown tool: {name}")
            result = getattr(self.tools, name)(**args)
            if name in ("open_ssh_panel", "panel_bootstrap", "panel_request", "panel_private_input"):
                return self.result(request_id, result)
            text = json.dumps(result, ensure_ascii=False, indent=2)
            return self.result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except PanelKeyExpired as exc:
            return self.tool_error(request_id, str(exc), code="PANEL_KEY_EXPIRED")
        except (urllib.error.URLError, TimeoutError) as exc:
            return self.tool_error(request_id, f"Bridge request failed: {exc}")
        except Exception as exc:
            return self.tool_error(request_id, str(exc))

    def result(self, request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def error(self, request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def tool_error(self, request_id, message, *, code=None):
        payload = {"ok": False, "error": message}
        if code:
            payload["code"] = code
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return self.result(request_id, {"content": [{"type": "text", "text": text}], "isError": True})


def main():
    parser = argparse.ArgumentParser(description="MCP server for the local Codex SSH terminal bridge.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--state-dir", help="Override local state/config directory for this MCP server and spawned bridge.")
    args = parser.parse_args()
    if args.state_dir:
        configure_state_home(args.state_dir)
        refresh_state_files()
    McpServer(TerminalTools(args.base_url)).serve()


if __name__ == "__main__":
    main()
