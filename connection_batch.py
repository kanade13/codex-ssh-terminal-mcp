"""Four-host login: one password-only host plus a shared-OTP bastion group."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import threading
import time
import uuid

from batch_auth import SharedLoginCode
from profiles import get_profile, profile_to_command, ssh_config_arguments
from state_paths import state_dir, state_file


HF_PROFILES = ("8HFA40", "hf6-A800", "hf7-A40")
PASSWORD_PROFILE = "HF3090"
_batch_file = state_file("batch-profiles.json")
if _batch_file.exists():
    _batch_config = json.loads(_batch_file.read_text(encoding="utf-8"))
    _mfa = _batch_config.get("mfa_profiles", [])
    _password = _batch_config.get("password_profile")
    if not isinstance(_mfa, list) or len(_mfa) != 3 or not all(isinstance(n, str) and n for n in _mfa) or len(set(_mfa)) != 3:
        raise ValueError("batch-profiles.json needs three distinct mfa_profiles")
    if not isinstance(_password, str) or not _password or _password in _mfa:
        raise ValueError("batch-profiles.json needs a distinct password_profile")
    HF_PROFILES, PASSWORD_PROFILE = tuple(_mfa), _password
BATCH_PROFILES = (*HF_PROFILES, PASSWORD_PROFILE)
CONTROL_PERSIST_SECONDS = 12 * 60 * 60


def control_path(profile_name, command):
    # Keep Unix socket paths well below the platform limit, in a private directory.
    directory = state_dir() / "control"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    identity = json.dumps([profile_name, command], ensure_ascii=False)
    name = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return str(directory / (name + ".sock"))


def managed_command(profile_name, profile, *, jump_path=None):
    command = profile_to_command(profile)
    if Path(command[0]).name != "ssh" or profile.get("mode") == "command":
        raise ValueError("四机联动需要已保存的主机连接配置")
    socket_path = control_path(profile_name, command)
    options = [
        "-o", "ControlMaster=auto", "-o", f"ControlPersist={CONTROL_PERSIST_SECONDS}",
        "-o", f"ControlPath={socket_path}",
        # WSL 2.7.10 mirrored networking can lose traffic when OpenSSH
        # switches QoS classes after authentication / mux backgrounding.
        "-o", "IPQoS=none",
    ]
    if jump_path:
        # -W uses the authenticated 8-host master. BatchMode prevents a new
        # bastion login if that master disappears while opening a target.
        proxy = shlex.join([
            "ssh", *ssh_config_arguments(), "-S", jump_path, "-o", "BatchMode=yes",
            "-o", "ControlMaster=no", "-o", "IPQoS=none",
            "-o", "ProxyCommand=false", "-W", "%h:%p", HF_PROFILES[0],
        ])
        options += ["-o", f"ProxyCommand={proxy}"]
        # An explicit -J would take precedence over ProxyCommand.
        cleaned = [command[0]]
        index = 1
        while index < len(command):
            if command[index] == "-J":
                index += 2
            else:
                cleaned.append(command[index])
                index += 1
        command = cleaned
    return [command[0], *options, *command[1:]], socket_path


def reuse_command(profile, socket_path, *, terminal=False, remote_command=None):
    """Open only a channel on an existing master; never fall back to login."""
    if not socket_path or profile.get("mode") == "command":
        raise ValueError("需要已认证的 SSH 主连接")
    base = profile_to_command(profile)
    # -J overrides ProxyCommand, even when the latter was given first.
    cleaned = []
    index = 1
    while index < len(base):
        if base[index] == "-J":
            index += 2
        else:
            cleaned.append(base[index])
            index += 1
    command = [
        base[0], "-S", str(socket_path),
        "-o", "ControlMaster=no", "-o", "BatchMode=yes",
        "-o", "ProxyCommand=false", "-o", "ProxyJump=none",
        "-o", "PermitLocalCommand=no", "-o", "ClearAllForwardings=yes",
        "-o", "IPQoS=none", "-o", "EscapeChar=none",
        "-o", "LogLevel=ERROR",
        "-tt" if terminal else "-T", *cleaned,
    ]
    if remote_command is not None:
        command.append(remote_command)
    return command


def master_ready(path, profile_name):
    if not path or not Path(path).exists():
        return False
    try:
        result = subprocess.run(
            ["ssh", *ssh_config_arguments(), "-S", path, "-O", "check", profile_name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=2, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def channel_ready(path, profile_name):
    """Verify a remote channel, not just a responsive local mux socket."""
    if not path or not Path(path).exists():
        return False
    try:
        result = subprocess.run(
            [
                "ssh", *ssh_config_arguments(), "-S", path, "-o", "ControlMaster=no",
                "-o", "BatchMode=yes", "-o", "ProxyCommand=false",
                "-o", "IPQoS=none", "-T", profile_name,
                "printf '__CODEX_SSH_READY__\\n'",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=8, check=False,
        )
        return result.returncode == 0 and b"__CODEX_SSH_READY__" in result.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return False


class ConnectionBatch:
    def __init__(self, manager):
        self.manager = manager
        self.id = "batch_" + uuid.uuid4().hex[:12]
        self.code = SharedLoginCode()
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.session_ids = {}
        self.connected = []
        self.errors = {}
        self.phase = "connecting"
        self.message = f"正在并行连接 {PASSWORD_PROFILE} 和 {HF_PROFILES[0]}，随后连接其余两台"
        self.thread = None

    def start(self):
        profiles = {name: get_profile(name) for name in BATCH_PROFILES}
        if not all(profiles.values()):
            raise ValueError("四机联动需要这些连接配置：" + "、".join(BATCH_PROFILES))
        self.profiles = profiles
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _session(self, name, jump_path=None):
        command, socket_path = managed_command(name, self.profiles[name], jump_path=jump_path)
        with self.manager.lock:
            existing = next((
                session for session in self.manager.sessions.values()
                if session.profile_name == name and session.control_path == socket_path
                and session.status(redact=True)["running"]
            ), None)
        if existing:
            if existing.auth_broker and name in HF_PROFILES:
                self._enroll(existing.auth_broker, name)
            session_id = existing.session_id
        else:
            result = self.manager.start_session(
                command, name, shared_code=self.code if name in HF_PROFILES else None,
                control_socket=socket_path,
            )
            session_id = result["sessionId"]
        with self.lock:
            self.session_ids[name] = session_id
        return self.manager.resolve(session_id)

    def _enroll(self, broker, name):
        entry = broker.entries.get(name)
        if not entry:
            raise ValueError("验证码联动需要对应的本机密码配置")
        self.code.enroll(broker, (entry["user"], entry["host"].lower()))

    def _wait_ready(self, name, session, deadline):
        while time.monotonic() < deadline and not self.cancelled.is_set():
            if not session.status(redact=True)["running"]:
                raise RuntimeError(f"{name} 连接已结束，请查看该服务器的终端输出")
            if master_ready(session.control_path, name):
                if not channel_ready(session.control_path, name):
                    with self.lock:
                        self.errors[name] = "认证已通过，但远端命令通道无响应，请重新连接"
                    raise RuntimeError("Remote channel did not respond")
                session.authenticated = True
                with self.lock:
                    if name not in self.connected:
                        self.connected.append(name)
                return True
            self.cancelled.wait(0.2)
        raise RuntimeError(f"{name} 连接等待超时或已取消")

    def _connect_password_host(self):
        if self.cancelled.is_set():
            return
        try:
            session = self._session(PASSWORD_PROFILE)
            self._wait_ready(PASSWORD_PROFILE, session, time.monotonic() + 180)
        except Exception:
            with self.lock:
                self.errors.setdefault(PASSWORD_PROFILE, "密码登录未完成，请查看该服务器的终端输出")

    def _run(self):
        # This worker never joins the shared-code pool. A slow password-only login
        # must not delay the bastion or the other two MFA logins (or vice versa).
        password_worker = threading.Thread(target=self._connect_password_host, daemon=True)
        password_worker.start()
        try:
            if self.cancelled.is_set():
                return
            jump = self._session(HF_PROFILES[0])
            self._wait_ready(HF_PROFILES[0], jump, time.monotonic() + 180)
            with self.lock:
                self.message = f"{HF_PROFILES[0]} 已就绪，正在并行连接其余两台"
            targets = {}
            for name in HF_PROFILES[1:]:
                if self.cancelled.is_set():
                    return
                try:
                    targets[name] = self._session(name, jump.control_path)
                except Exception:
                    with self.lock:
                        self.errors[name] = "启动失败，请查看连接配置"
            # All targets have been started before waiting for either to finish.
            deadline = time.monotonic() + 180
            while targets and not self.cancelled.is_set() and time.monotonic() < deadline:
                for name, session in list(targets.items()):
                    if not session.status(redact=True)["running"]:
                        with self.lock:
                            self.errors[name] = "连接失败，请查看该服务器的终端输出"
                        targets.pop(name)
                    elif master_ready(session.control_path, name):
                        if channel_ready(session.control_path, name):
                            session.authenticated = True
                            with self.lock:
                                self.connected.append(name)
                        else:
                            with self.lock:
                                self.errors[name] = "认证已通过，但远端命令通道无响应，请重新连接"
                        targets.pop(name)
                if targets:
                    self.cancelled.wait(0.2)
            with self.lock:
                for name in targets:
                    self.errors[name] = "等待认证超时"
        except Exception:
            with self.lock:
                self.errors.setdefault(HF_PROFILES[0], "跳板机连接未完成，请查看该服务器的终端输出")
                for name in HF_PROFILES[1:]:
                    if name not in self.session_ids:
                        self.errors.setdefault(name, "等待跳板机连接成功后重试")
        finally:
            # Clear the OTP as soon as the three MFA logins finish, even while
            # the independent password-only connection is still pending.
            self.code.close()
            password_worker.join()
            with self.lock:
                if self.cancelled.is_set():
                    self.phase = "cancelled"
                    self.message = "本次四机连接已取消"
                elif len(self.connected) == len(BATCH_PROFILES):
                    self.phase = "complete"
                    self.message = "四台服务器均已连接"
                else:
                    self.phase = "partial" if self.connected else "failed"
                    self.message = "部分连接未完成，可重新点击连接四台" if self.connected else "连接未完成，请查看终端输出后重试"

    def status(self):
        with self.lock:
            ids = dict(self.session_ids)
            result = {
                "id": self.id, "phase": self.phase, "message": self.message,
                "profiles": list(BATCH_PROFILES), "sessionIds": ids,
                "connectedProfiles": list(self.connected), "errors": dict(self.errors),
            }
        pending = []
        live = []
        for name, session_id in ids.items():
            session = self.manager.resolve(session_id)
            if name in result["connectedProfiles"] and session.status(redact=True)["running"]:
                live.append(name)
            broker = session.auth_broker
            if broker and broker.shared_code_pending:
                pending.append(name)
        result["connectedProfiles"] = live
        if result["phase"] != "connecting" and len(live) < len(self.connected):
            result["message"] = "部分连接已断开，可重新点击连接四台" if live else "连接已断开，可重新点击连接四台"
            if result["phase"] == "complete":
                result["phase"] = "partial"
        result["needsCode"] = bool(pending)
        result["pendingProfiles"] = pending
        if result["phase"] == "connecting" and HF_PROFILES[0] in live:
            waiting = [name for name in BATCH_PROFILES if name not in live and name not in result["errors"]]
            result["message"] = f"{HF_PROFILES[0]} 已就绪，正在连接：" + "、".join(waiting) if waiting else "正在完成连接检查"
        if result["phase"] != "connecting" and result["errors"]:
            result["message"] += " · " + "；".join(f"{name}: {error}" for name, error in result["errors"].items())
        return result

    def cancel(self):
        self.cancelled.set()
        self.code.close()
        with self.lock:
            self.phase = "cancelled"
            self.message = "本次四机连接已取消"
