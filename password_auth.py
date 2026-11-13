"""Local-only OpenSSH askpass transport. Secrets never enter terminal history."""
import hmac
import json
import os
import queue
import re
import secrets
import shlex
import socket
import stat
import sys
import tempfile
import threading
from pathlib import Path

from state_paths import state_file


PASSWORD_PROMPTS = (
    re.compile(r"^\((?P<user>[^@\r\n]+)@(?P<host>[^)\r\n]+)\)\s*Password:\s*$", re.I),
    re.compile(r"^(?P<user>[^@\r\n]+)@(?P<host>[^\r\n]+)'s password:\s*$", re.I),
)
OTP_PROMPT = re.compile(
    r"^\((?P<user>[^@\r\n]+)@(?P<host>[^)\r\n]+)\)\s*Verification code:\s*$", re.I
)


def load_password_profiles(path=None):
    path = Path(path) if path else state_file("passwords.json")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    except OSError:
        raise ValueError("Cannot open the local passwords.json file") from None
    with os.fdopen(fd, "r", encoding="utf-8-sig") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("passwords.json must be owned by the current user with mode 600")
        try:
            data = json.load(handle)
        except (ValueError, UnicodeError):
            raise ValueError("passwords.json is not valid UTF-8 JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("profiles", {}), dict):
        raise ValueError("passwords.json must contain a profiles object")
    if data.get("enabled", True) is not True:
        return {}
    profiles = data.get("profiles", {})
    for name, entry in profiles.items():
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("host", "user", "password")):
            raise ValueError("Each password profile needs host, user and password strings")
        if not entry["host"] or not entry["user"] or any(c in entry["password"] for c in "\r\n\x00"):
            raise ValueError("Invalid password profile fields; passwords must be single-line strings")
        jumps = entry.get("jump_profiles", [])
        if not isinstance(jumps, list) or any(not isinstance(j, str) or j not in profiles for j in jumps):
            raise ValueError("jump_profiles must reference existing password profiles")
    return profiles


def receive_json(sock):
    data = bytearray()
    while len(data) < 16384:
        chunk = sock.recv(4096)
        if not chunk:
            raise ValueError("Authentication helper disconnected")
        data.extend(chunk)
        if b"\n" in data:
            return json.loads(bytes(data).split(b"\n", 1)[0])
    raise ValueError("Authentication helper message too large")


def send_json(sock, value):
    sock.sendall(json.dumps(value).encode("utf-8") + b"\n")


class AskpassBroker:
    def __init__(self, entries, publish, timeout=180):
        self.entries = entries
        self.publish = publish
        self.timeout = timeout
        self.attempted = set()
        self.lock = threading.RLock()
        self._pending = None
        self._pending_identity = None
        self._pending_info = None
        self.shared_code = None
        self._closed = threading.Event()
        self.token = secrets.token_urlsafe(32)
        self.directory = tempfile.TemporaryDirectory(prefix="codex-ssh-auth-")
        self.socket_path = str(Path(self.directory.name) / "auth.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        self.helper = Path(self.directory.name) / "askpass"
        self.helper.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
            + shlex.quote(str(Path(__file__).resolve())) + ' --askpass "$@"\n', encoding="utf-8"
        )
        self.helper.chmod(0o700)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @classmethod
    def for_profile(cls, profile_name, publish):
        profiles = load_password_profiles()
        if profile_name not in profiles:
            return None
        names = []

        def visit(name):
            if name in names:
                return
            names.append(name)
            for jump in profiles[name].get("jump_profiles", []):
                visit(jump)

        visit(profile_name)
        return cls({name: profiles[name] for name in names}, publish)

    @property
    def pending(self):
        with self.lock:
            return self._pending is not None

    @property
    def pending_info(self):
        with self.lock:
            return dict(self._pending_info) if self._pending_info else None

    @property
    def autofill_enabled(self):
        return any(entry.get("password") for entry in self.entries.values())

    def otp_identity(self, prompt, hint=""):
        if hint in ("confirm", "none"):
            return None
        match = OTP_PROMPT.fullmatch(prompt)
        if not match:
            return None
        identity = (match["user"], match["host"].lower())
        if any((entry["user"], entry["host"].lower()) == identity for entry in self.entries.values()):
            return identity
        return None

    @property
    def shared_code_pending(self):
        with self.lock:
            return bool(
                self._pending is not None and not self._pending.full()
                and self.shared_code
                and self.shared_code.accepts(self, self._pending_identity)
            )

    def deliver_shared_code(self):
        with self.lock:
            if not self.shared_code_pending:
                return
            value = self.shared_code.claim(self, self._pending_identity)
            if value is not None:
                self._pending.put_nowait(value)

    def environment(self):
        return dict(
            os.environ,
            SSH_ASKPASS=str(self.helper),
            SSH_ASKPASS_REQUIRE="force",
            CODEX_SSH_AUTH_SOCKET=self.socket_path,
            CODEX_SSH_AUTH_TOKEN=self.token,
        )

    def submit(self, value, request_id=None):
        with self.lock:
            if request_id is not None and (
                not self._pending_info or self._pending_info["id"] != request_id
            ):
                raise ValueError("认证提示已结束或已更换，请使用当前提示")
            if self._pending is None:
                return False
            value = value.rstrip("\r\n")
            if any(c in value for c in "\r\n\x00"):
                raise ValueError("Authentication input must be a single line")
            try:
                self._pending.put_nowait(value)
            except queue.Full:
                raise ValueError("Authentication response already submitted") from None
            return True

    def cancel(self):
        with self.lock:
            if self._pending is not None:
                try:
                    self._pending.put_nowait(None)
                except queue.Full:
                    pass

    def close(self):
        with self.lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self.cancel()
            self._pending_info = None
            self.entries.clear()
        self.listener.close()
        self.directory.cleanup()

    def _auto_password(self, prompt, hint):
        if hint in ("confirm", "none"):
            return None
        match = next((m for regex in PASSWORD_PROMPTS if (m := regex.fullmatch(prompt))), None)
        if not match:
            return None
        with self.lock:
            entries = list(self.entries.items())
        for name, entry in entries:
            identity = (entry["user"], entry["host"].lower())
            if identity != (match["user"], match["host"].lower()):
                continue
            if not entry["password"] or identity in self.attempted:
                return None
            self.attempted.add(identity)
            self.publish("system", f"\r\n[auth] 已自动填写 {name} 的密码。\r\n")
            return entry["password"]
        return None

    def _answer(self, prompt, hint):
        if hint == "none":
            self.publish("system", prompt + "\r\n")
            return ""
        answer = self._auto_password(prompt, hint)
        if answer is not None:
            return answer
        identity = self.otp_identity(prompt, hint)
        if self.shared_code and identity:
            answer = self.shared_code.claim(self, identity)
            if answer is not None:
                self.publish("system", "\r\n[auth] 已使用本次三机连接的验证码。\r\n")
                return answer
        response = queue.Queue(maxsize=1)
        with self.lock:
            if self._closed.is_set():
                return None
            self._pending = response
            self._pending_identity = identity
            kind = "code" if identity else "password"
            if hint == "confirm" or "Please type 'yes', 'no' or the fingerprint" in prompt:
                kind = "host_key"
            self._pending_info = {
                "id": secrets.token_hex(12), "kind": kind, "prompt": prompt,
            }
        self.publish("auth_prompt", "\r\n" + prompt + " ")
        # Covers a code submitted between the first claim and queue creation.
        self.deliver_shared_code()
        try:
            return response.get(timeout=self.timeout)
        except queue.Empty:
            self.publish("system", "\r\nAuthentication input timed out. Reconnect to try again.\r\n")
            return None
        finally:
            with self.lock:
                self._pending = None
                self._pending_identity = None
                self._pending_info = None
            self.publish("auth_response", "\r\n")

    def _serve(self):
        while not self._closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(10)
                    request = receive_json(connection)
                    if not isinstance(request, dict) or not hmac.compare_digest(str(request.get("token", "")), self.token):
                        send_json(connection, {"ok": False})
                        continue
                    prompt = request.get("prompt", "")
                    if not isinstance(prompt, str) or len(prompt) > 8192:
                        send_json(connection, {"ok": False})
                        continue
                    answer = self._answer(prompt, request.get("hint", ""))
                    send_json(connection, {"ok": answer is not None, "answer": answer or ""})
                except (OSError, ValueError, TypeError):
                    continue


def askpass_main():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(190)
            connection.connect(os.environ["CODEX_SSH_AUTH_SOCKET"])
            send_json(connection, {
                "token": os.environ["CODEX_SSH_AUTH_TOKEN"],
                "prompt": sys.argv[2] if len(sys.argv) > 2 else "",
                "hint": os.environ.get("SSH_ASKPASS_PROMPT", ""),
            })
            result = receive_json(connection)
        if not result.get("ok"):
            return 1
        sys.stdout.write(result["answer"] + "\n")
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(askpass_main())
