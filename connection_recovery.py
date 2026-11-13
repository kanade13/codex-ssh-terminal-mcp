"""Restore terminal channels without replaying commands or retaining OTPs."""
import threading
import time

from connection_batch import PASSWORD_PROFILE, channel_ready, master_ready, reuse_command
from profiles import get_profile


class ConnectionRecovery:
    def __init__(self, manager, interval=5):
        self.manager, self.interval = manager, interval
        self.stopped = threading.Event()
        self.thread = None
        self.next_attempt = {}
        self.attempts = {}
        self.password_relogin = set()

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(1)

    def _run(self):
        while not self.stopped.wait(self.interval):
            try:
                self.tick()
            except Exception:
                # A transient profile or subprocess error must not kill recovery.
                pass

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        with self.manager.lock:
            sessions = list(self.manager.sessions.values())
        latest = {s.profile_name or s.session_id: s for s in sessions}
        for session in sessions:
            if self.stopped.is_set():
                return
            if not session.auto_reconnect or not session.control_path:
                continue
            if latest[session.profile_name or session.session_id] is not session:
                continue
            if self.next_attempt.get(session.session_id, 0) > now:
                continue
            status = session.status(redact=True)
            if status["running"]:
                if session.authenticated:
                    session.recovery_state = "ready"
                    self.password_relogin.discard(session.session_id)
                    continue
                # The batch worker owns authentication and its one-code pool.
                batch = self.manager.connection_batch
                if batch and batch.phase == "connecting":
                    continue
                if status["authPromptPending"]:
                    continue
                if master_ready(session.control_path, session.profile_name) and channel_ready(session.control_path, session.profile_name):
                    session.authenticated = True
                    session.recovery_state = "ready"
                continue
            # Do not restart a PTY until its old reader and askpass are finished.
            if session.reader_thread and session.reader_thread.is_alive():
                continue
            count = self.attempts.get(session.session_id, 0) + 1
            self.attempts[session.session_id] = count
            self.next_attempt[session.session_id] = now + min(60, 5 * 2 ** min(count - 1, 4))
            if not master_ready(session.control_path, session.profile_name):
                if session.profile_name == PASSWORD_PROFILE and session.session_id not in self.password_relogin:
                    self.password_relogin.add(session.session_id)
                    try:
                        self.manager.restore_terminal(session, session.command, authenticated=False)
                    except (OSError, RuntimeError, ValueError):
                        pass
                    continue
                session.recovery_state = "needs_auth"
                session.recovery_message = "SSH 主连接已失效，请重新连接并完成认证；原命令不会重跑。"
                continue
            if not channel_ready(session.control_path, session.profile_name):
                session.recovery_state = "retrying"
                session.recovery_message = "主连接暂时无响应，正在等待恢复；不会重跑原命令。"
                continue
            profile = get_profile(session.profile_name)
            if not profile:
                continue
            command = reuse_command(profile, session.control_path, terminal=True)
            try:
                restored = self.manager.restore_terminal(session, command)
            except (OSError, RuntimeError, ValueError):
                session.recovery_state = "retrying"
                continue
            if restored:
                self.attempts[session.session_id] = 0
                session.recovery_message = "已复用认证主连接恢复终端；这是新 shell，原命令没有重跑。"
