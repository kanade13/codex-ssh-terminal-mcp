"""Temporary companion for an already-running bridge; never restart that bridge.

New bridges own recovery themselves. This companion lets existing authenticated
masters stay open for this 12-hour window and repairs old batch terminals using
the old bridge's public API, without touching a healthy shell or replaying input.
"""
import argparse
import fcntl
import json
import math
import os
import shlex
import subprocess
import time

from connection_batch import BATCH_PROFILES, channel_ready, control_path, master_ready, reuse_command
from mcp_panel import bridge_origin
from profiles import get_profile, profile_to_command
from state_paths import chmod_private_file, configure_state_home, state_file


class LegacyMaintenance:
    def __init__(self, tools, duration=43200):
        self.tools = tools
        self.deadline = time.monotonic() + duration
        self.seen_connected = set()
        self.disabled = {}
        self.keepers = {}
        self.next_recovery = 0
        self.message = "正在检查已连接的服务器"

    def _manual_stop(self, session):
        data = self.tools._get("/api/snapshot", {"sessionId": session["sessionId"], "limit": 20}, timeout=3)
        return any(e.get("type") == "system" and "Session stopped." in e.get("text", "")
                   for e in data.get("events", []))

    def _keep_master(self, name, profile, path):
        old = self.keepers.get(name)
        if old and old.poll() is None:
            return
        if old and old.stdin:
            old.stdin.close()
        if not master_ready(path, name):
            return
        seconds = max(1, math.ceil(self.deadline - time.monotonic()))
        remote = "bash --noprofile --norc -c " + shlex.quote(f"read -r -t {seconds} _codex_keepalive || :")
        self.keepers[name] = subprocess.Popen(
            reuse_command(profile, path, remote_command=remote),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def step(self):
        status = self.tools._get("/api/status", {"redact": "1"}, timeout=3)
        latest = {s.get("profileName"): s for s in status.get("sessions", []) if s.get("profileName") in BATCH_PROFILES}
        for name, session in latest.items():
            if session.get("running") and session.get("connected"):
                self.seen_connected.add(name)
                if self.disabled.get(name) != session["sessionId"]:
                    self.disabled.pop(name, None)
            elif name in self.seen_connected and not session.get("running") and self._manual_stop(session):
                self.disabled[name] = session["sessionId"]
        profiles, paths = {}, {}
        for name, session in latest.items():
            if name not in self.seen_connected or name in self.disabled:
                continue
            profile = get_profile(name)
            if not profile or profile.get("mode") == "command":
                continue
            profiles[name] = profile
            paths[name] = control_path(name, profile_to_command(profile))
            self._keep_master(name, profile, paths[name])
        if status.get("features", {}).get("connectionRecovery"):
            self.message = "新版后台已接管自动恢复；现有主连接仍保留至本次 12 小时窗口结束"
            return
        dead = [name for name, s in latest.items() if name in self.seen_connected
                and name not in self.disabled and not s.get("running") and s.get("managedConnection")]
        if not dead:
            self.message = "已启用 12 小时主连接保持和兼容自动恢复"
            return
        if self.disabled or set(latest) != set(BATCH_PROFILES):
            self.message = "存在用户主动断开的连接；兼容恢复不会替用户重新连接它们"
            return
        if any(s.get("authPromptPending") for s in latest.values()) or (status.get("connectionBatch") or {}).get("phase") == "connecting":
            self.message = "正在等待现有连接或私密认证完成"
            return
        if time.monotonic() < self.next_recovery:
            return
        self.next_recovery = time.monotonic() + 30
        for name in dead:
            if name not in paths or not master_ready(paths[name], name):
                self.message = "主连接已失效，请在面板重新连接并完成认证"
                return
            if not channel_ready(paths[name], name):
                self.message = "主连接暂时无响应，稍后重试；原任务不会重跑"
                return
        # All healthy sessions are reused by ConnectionBatch._session. Only
        # dead terminals are replaced. No /api/stop or terminal-input calls.
        self.tools._post("/api/start", {"connectAll": True}, timeout=5)
        self.message = "正在复用现有认证恢复掉线终端；原任务不会重跑"

    def close(self):
        for proc in self.keepers.values():
            if proc.stdin:
                proc.stdin.close()  # EOF ends only our private remote read.
        for proc in self.keepers.values():
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.terminate()  # This client is never a ControlMaster.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18766")
    parser.add_argument("--hours", type=float, default=12)
    args = parser.parse_args()
    base = bridge_origin(args.base_url)
    configure_state_home(args.state_dir)
    from mcp_server import TerminalTools, refresh_state_files
    refresh_state_files()
    lock_path = state_file("maintenance.lock")
    with lock_path.open("a") as lock:
        chmod_private_file(lock_path)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        duration = max(60, min(43200, args.hours * 3600))
        worker = LegacyMaintenance(TerminalTools(base), duration)
        report = state_file("maintenance-status.json")
        expires = int((time.time() + duration) * 1000)
        try:
            while time.monotonic() < worker.deadline:
                try:
                    worker.step()
                except Exception:
                    worker.message = "SSH 后台暂时不可达，等待恢复；现有连接不作改动"
                report.write_text(json.dumps({"version": "0.8.0", "pid": os.getpid(), "running": True,
                    "expiresAt": expires, "message": worker.message,
                    "heldProfiles": [name for name, proc in worker.keepers.items() if proc.poll() is None]}, ensure_ascii=False) + "\n")
                chmod_private_file(report)
                time.sleep(5)
        finally:
            worker.close()
            report.write_text(json.dumps({"version": "0.8.0", "running": False, "expiresAt": expires}) + "\n")
            chmod_private_file(report)


if __name__ == "__main__":
    main()
