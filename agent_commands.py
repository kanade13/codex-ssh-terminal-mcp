"""Per-MCP-client command channels. Never write to the user's terminal PTY."""
import codecs
from collections import deque
import os
import shlex
import signal
import subprocess
import threading
import time
import uuid

from connection_batch import control_path, master_ready, reuse_command
from profiles import get_profile, profile_to_command


def now_ms():
    return int(time.time() * 1000)


class CommandJob:
    def __init__(self, command, session_id, profile_name, argv):
        self.id = "cmd_" + uuid.uuid4().hex[:12]
        self.session_id, self.profile_name = session_id, profile_name
        self.command = command
        self.started_at, self.ended_at = now_ms(), None
        self.returncode = None
        self.history = deque(maxlen=256)
        self.seq, self.cursor = 1, 0
        self.lock = threading.RLock()
        # ControlMaster=no ensures this process can never own/kill the master.
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, start_new_session=True,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _publish(self, kind, text):
        with self.lock:
            self.history.append({"seq": self.seq, "ts": now_ms(), "type": kind, "text": text})
            self.seq += 1

    def _read(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                raw = self.proc.stdout.read(4096)
                if not raw:
                    break
                self._publish("output", decoder.decode(raw))
            tail = decoder.decode(b"", final=True)
            if tail:
                self._publish("output", tail)
        finally:
            code = self.proc.wait()
            self.proc.stdout.close()
            self.proc.stdin.close()
            with self.lock:
                self.returncode, self.ended_at = code, now_ms()
                self._publish("system", f"Command exited with code {code}.\n")

    def status(self, *, after=None, full=False, peek=False, limit=80, events=False):
        limit = max(1, min(1000, int(limit)))
        with self.lock:
            cursor = 0 if full else self.cursor if after is None else int(after)
            items = [dict(e) for e in self.history if e["seq"] > cursor]
            result = {
                "ok": True, "channel": "agent", "commandId": self.id,
                "sessionId": self.session_id, "profileName": self.profile_name,
                "running": self.returncode is None, "returncode": self.returncode,
                "startedAt": self.started_at, "endedAt": self.ended_at,
                "nextSeq": self.seq,
                "oldestSeq": self.history[0]["seq"] if self.history else self.seq,
                "truncated": bool(self.history and cursor < self.history[0]["seq"] - 1),
            }
            if events:
                result["events"] = items[-limit:]
            else:
                from server import events_to_lines
                lines, skipped = events_to_lines(items, compact=True)
                result.update(lines=lines[-limit:], skippedProgressUpdates=skipped)
            if not peek and not full:
                self.cursor = self.seq - 1
            return result

    def control(self, name):
        keys = {"ctrl-c": b"\x03", "ctrl-d": b"\x04", "enter": b"\r"}
        if name not in keys:
            raise ValueError("Unknown command control")
        with self.lock:
            if self.returncode is not None or self.proc.poll() is not None:
                raise ValueError("独立命令已经结束；不会向面板终端发送控制键")
            self.proc.stdin.write(keys[name])
            self.proc.stdin.flush()
        return {"ok": True, "commandId": self.id, "channel": "agent"}

    def cancel(self):
        try:
            self.control("ctrl-c")
        except (ValueError, BrokenPipeError):
            return self.status(peek=True)

        def close_channel_if_needed():
            self.thread.join(2)
            if self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        threading.Thread(target=close_channel_if_needed, daemon=True).start()
        return {"ok": True, "commandId": self.id, "cancelRequested": True,
                "message": "仅中断该命令通道；长任务请检查远端退出状态。"}


class AgentCommands:
    def __init__(self):
        self.jobs, self.latest = {}, {}
        self.lock = threading.RLock()

    def start(self, command, session, expected_profile_name=None):
        if not isinstance(command, str) or not command.strip() or len(command) > 262144:
            raise ValueError("command must contain 1..262144 characters")
        name = session.get("profileName")
        if expected_profile_name and name != expected_profile_name:
            raise ValueError("session/profile mismatch")
        if session.get("agentInputEnabled") is False or session.get("authPromptPending"):
            raise PermissionError("Codex 输入已暂停或 SSH 正在等待私密认证")
        profile = get_profile(name)
        if not profile or profile.get("mode") == "command":
            raise ValueError("独立命令通道需要已保存的 SSH 主机配置")
        path = control_path(name, profile_to_command(profile))
        if not master_ready(path, name):
            raise RuntimeError("SSH 主连接不可用，请在面板完成连接/验证码；不会改用共享终端")
        # Each invocation gets a fresh remote shell. No commands are replayed.
        remote = "bash -lc " + shlex.quote(command)
        argv = reuse_command(profile, path, terminal=True, remote_command=remote)
        with self.lock:
            if sum(job.returncode is None for job in self.jobs.values()) >= 8:
                raise ValueError("本聊天最多同时运行 8 条独立命令")
            for key in list(self.jobs):
                if len(self.jobs) < 32:
                    break
                if self.jobs[key].returncode is not None:
                    del self.jobs[key]
            job = CommandJob(command, session["sessionId"], name, argv)
            self.jobs[job.id] = job
            self.latest[job.session_id] = job.id
        return {"ok": True, "commandId": job.id, "sessionId": job.session_id,
                "profileName": name, "channel": "agent", "running": True,
                "hint": "用 command_status(command_id) 读取结果。每条命令为新 shell，请在命令内显式 cd/激活环境；不会写入面板终端。"}

    def resolve(self, command_id=None, session_id=None):
        with self.lock:
            if not command_id and session_id:
                command_id = self.latest.get(session_id)
            if not command_id and not session_id and len(self.latest) == 1:
                command_id = next(iter(self.latest.values()))
            job = self.jobs.get(command_id)
        if not job:
            raise ValueError("请指定本聊天 send_command 返回的 command_id；不会控制共享面板终端")
        return job
