import hashlib
import json
import os
import re
import select
import shlex
import signal
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from profiles import get_profile, ssh_config_arguments
from platform_support import assert_supported_platform
from state_paths import chmod_private_file, state_file


ROOT = Path(__file__).resolve().parent
UPLOAD_STATE_FILE = state_file("upload-state.json", legacy_name=".upload-state.json")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LOG_TRUNCATION_MARKER = "\n...[upload log entry truncated]...\n"


def now_ms():
    return int(time.time() * 1000)


def shell_join(parts):
    return " ".join(shlex.quote(str(part)) for part in parts)


def clean_log_text(text):
    return ANSI_RE.sub("", str(text))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class UploadRequest:
    def __init__(
        self,
        local_path,
        profile_name,
        remote_path,
        if_exists="fail",
        method="scp",
        checksum="none",
        *,
        upload_id=None,
        validate=True,
        state=None,
        log_limit=120,
        log_entry_max_chars=8000,
    ):
        self.log_limit = log_limit
        self.log_entry_max_chars = max(200, int(log_entry_max_chars))

        if state is not None:
            self._load_state(state)
            self.log = deque(
                [self._compact_log_entry(item) for item in state.get("log", [])[-log_limit:]],
                maxlen=log_limit,
            )
            self.pid = None
            self.master_fd = None
            self.last_persisted_at = 0
            return

        path = Path(local_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"local file not found: {local_path}")
        if "\n" in str(remote_path) or "\r" in str(remote_path):
            raise ValueError("remote_path must not contain newlines")

        if validate:
            profile = get_profile(profile_name)
            if not profile:
                raise ValueError(f"profile not found: {profile_name}")
            if profile.get("mode") != "ssh":
                raise ValueError("file upload supports host/port/user profiles, including optional ProxyJump, not full command profiles")

        self.id = upload_id or "up_" + uuid.uuid4().hex[:12]
        self.local_path = str(path)
        self.filename = path.name
        self.size = path.stat().st_size
        self.profile_name = profile_name
        self.remote_path = str(remote_path)
        self.if_exists = if_exists
        self.method = method
        self.checksum = checksum
        self.status = "pending_confirmation"
        self.created_at = now_ms()
        self.updated_at = self.created_at
        self.started_at = None
        self.ended_at = None
        self.pid = None
        self.returncode = None
        self.master_fd = None
        self.log = deque(maxlen=log_limit)
        self.error = None
        self.last_persisted_at = 0
        self.verification = {
            "size": {"expected": self.size, "actual": None, "ok": None},
            "sha256": {"expected": None, "actual": None, "ok": None} if checksum == "sha256" else None,
        }

    def _load_state(self, state):
        self.id = state["id"]
        self.local_path = state.get("localPath")
        self.filename = state.get("filename")
        self.size = state.get("size")
        self.profile_name = state.get("profileName")
        self.remote_path = state.get("remotePath")
        self.if_exists = state.get("ifExists", "fail")
        self.method = state.get("method", "scp")
        self.checksum = state.get("checksum", "none")
        self.status = state.get("status", "failed")
        self.created_at = state.get("createdAt") or now_ms()
        self.updated_at = state.get("updatedAt") or self.created_at
        self.started_at = state.get("startedAt")
        self.ended_at = state.get("endedAt")
        self.returncode = state.get("returncode")
        self.error = state.get("error")
        self.verification = state.get("verification") or {
            "size": {"expected": self.size, "actual": None, "ok": None},
            "sha256": None,
        }
        if self.status == "running":
            self.status = "interrupted"
            self.ended_at = now_ms()
            self.updated_at = self.ended_at
            self.error = "bridge process restarted while upload was running"

    def public(self, include_log=True):
        data = {
            "id": self.id,
            "filename": self.filename,
            "size": self.size,
            "profileName": self.profile_name,
            "remotePath": self.remote_path,
            "ifExists": self.if_exists,
            "method": self.method,
            "checksum": self.checksum,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "returncode": self.returncode,
            "error": self.error,
            "verification": self.verification,
        }
        if include_log:
            data["log"] = list(self.log)
        return data

    def to_state(self):
        return self.public(include_log=True)

    def append_log(self, text):
        text = clean_log_text(text)
        if not text:
            return
        entry = self._compact_log_entry({"ts": now_ms(), "text": text})
        is_progress = "\r" in text and "\n" not in text
        if is_progress:
            entry["progress"] = True
            if self.log and self.log[-1].get("progress"):
                self.log[-1] = entry
            else:
                self.log.append(entry)
        else:
            self.log.append(entry)
        self.updated_at = now_ms()

    def _compact_log_entry(self, entry):
        entry = dict(entry)
        text = clean_log_text(entry.get("text", ""))
        if len(text) > self.log_entry_max_chars:
            keep = max(1, self.log_entry_max_chars - len(LOG_TRUNCATION_MARKER))
            text = text[:keep] + LOG_TRUNCATION_MARKER
            entry["truncated"] = True
        entry["text"] = text
        return entry


class TransferManager:
    def __init__(
        self,
        state_file=None,
        retention_days=14,
        log_limit=120,
        pending_ttl_days=7,
        log_entry_max_chars=8000,
        max_records=500,
    ):
        self.lock = threading.RLock()
        self.uploads = {}
        self.state_file = Path(state_file or UPLOAD_STATE_FILE)
        self.retention_days = max(1, int(retention_days))
        self.log_limit = max(20, int(log_limit))
        self.pending_ttl_days = max(1, int(pending_ttl_days))
        self.log_entry_max_chars = max(200, int(log_entry_max_chars))
        self.max_records = max(20, int(max_records))
        self._load()
        self._prune()
        self._save()

    def create_upload(self, local_path, profile_name, remote_path, if_exists="fail", method="scp", checksum="none"):
        if if_exists not in ("fail", "overwrite", "backup"):
            raise ValueError("if_exists must be one of: fail, overwrite, backup")
        if method not in ("scp", "rsync"):
            raise ValueError("method must be one of: scp, rsync")
        if checksum not in ("none", "sha256"):
            raise ValueError("checksum must be one of: none, sha256")
        upload = UploadRequest(
            local_path,
            profile_name,
            remote_path,
            if_exists,
            method,
            checksum,
            log_limit=self.log_limit,
            log_entry_max_chars=self.log_entry_max_chars,
        )
        with self.lock:
            self.uploads[upload.id] = upload
            self._prune_locked()
            self._save_locked()
        return upload.public()

    def list_uploads(self):
        with self.lock:
            self._prune_locked()
            return [item.public(include_log=False) for item in sorted(self.uploads.values(), key=lambda item: item.created_at)]

    def get_upload(self, upload_id):
        with self.lock:
            upload = self._get(upload_id)
            return upload.public()

    def confirm_upload(self, upload_id):
        with self.lock:
            upload = self._get(upload_id)
            if upload.status != "pending_confirmation":
                raise ValueError(f"upload is not pending confirmation: {upload.status}")
            command = self._build_command(upload)
            upload.status = "running"
            upload.started_at = now_ms()
            upload.updated_at = upload.started_at
            upload.append_log("Starting confirmed upload.\r\n")
            self._save_locked()
            assert_supported_platform()
            import pty

            pid, master_fd = pty.fork()
            if pid == 0:
                try:
                    os.execvp(command[0], command)
                except Exception as exc:
                    os.write(2, f"exec failed: {exc}\n".encode("utf-8", errors="replace"))
                    os._exit(127)
            upload.pid = pid
            upload.master_fd = master_fd
            threading.Thread(target=self._read_loop, args=(upload.id,), daemon=True).start()
            return upload.public()

    def reject_upload(self, upload_id):
        with self.lock:
            upload = self._get(upload_id)
            if upload.status != "pending_confirmation":
                raise ValueError(f"upload is not pending confirmation: {upload.status}")
            upload.status = "rejected"
            upload.ended_at = now_ms()
            upload.updated_at = upload.ended_at
            upload.append_log("Upload rejected by user.\r\n")
            self._save_locked()
            return upload.public()

    def cancel_upload(self, upload_id):
        with self.lock:
            upload = self._get(upload_id)
            if upload.status != "running":
                raise ValueError(f"upload is not running: {upload.status}")
            try:
                os.killpg(os.getpgid(upload.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            upload.status = "cancelled"
            upload.ended_at = now_ms()
            upload.updated_at = upload.ended_at
            upload.append_log("Upload cancelled.\r\n")
            self._save_locked()
            return upload.public()

    def write_input(self, upload_id, data, enter=False):
        with self.lock:
            upload = self._get(upload_id)
            if upload.status != "running" or upload.master_fd is None:
                raise ValueError("upload process is not running")
            text = str(data)
            if enter:
                text += "\r"
            os.write(upload.master_fd, text.encode("utf-8", errors="replace"))
            return {"ok": True}

    def _get(self, upload_id):
        upload = self.uploads.get(upload_id)
        if not upload:
            raise ValueError(f"upload not found: {upload_id}")
        return upload

    def _read_loop(self, upload_id):
        while True:
            with self.lock:
                upload = self.uploads.get(upload_id)
                if not upload or upload.master_fd is None:
                    return
                fd = upload.master_fd
                pid = upload.pid

            try:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited_pid = pid
                status = 0

            if waited_pid:
                with self.lock:
                    upload = self.uploads.get(upload_id)
                    if not upload:
                        return
                    self._finish_upload_locked(upload, status)
                return

            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(fd, 8192)
            except OSError as exc:
                with self.lock:
                    upload = self.uploads.get(upload_id)
                    if not upload or upload.status != "running":
                        return
                    try:
                        waited_pid, status = os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        waited_pid = pid
                        status = 0
                    if waited_pid:
                        self._finish_upload_locked(upload, status)
                    else:
                        upload.status = "failed"
                        upload.error = str(exc)
                        upload.ended_at = now_ms()
                        upload.updated_at = upload.ended_at
                        self._save_locked()
                return
            if data:
                text = data.decode("utf-8", errors="replace")
                with self.lock:
                    upload = self.uploads.get(upload_id)
                    if upload:
                        upload.append_log(text)
                        if now_ms() - upload.last_persisted_at > 1000:
                            self._save_locked()

    def _finish_upload_locked(self, upload, status):
        if os.WIFEXITED(status):
            upload.returncode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            upload.returncode = 128 + os.WTERMSIG(status)
        else:
            upload.returncode = status
        if upload.status == "running":
            upload.status = "completed" if upload.returncode == 0 else "failed"
        upload.ended_at = now_ms()
        upload.updated_at = upload.ended_at
        upload.append_log(f"\r\nUpload process exited with code {upload.returncode}.\r\n")
        self._parse_verification(upload)
        self._save_locked()

    def _build_command(self, upload):
        profile = get_profile(upload.profile_name)
        if not profile:
            raise ValueError(f"profile not found: {upload.profile_name}")
        if profile.get("mode") != "ssh":
            raise ValueError("file upload supports only host/port/user profiles, including optional ProxyJump")

        host = profile["sshHost"]
        user_host = f"{profile['sshUser']}@{host}" if profile.get("sshUser") else host
        port = str(profile.get("sshPort", 22))
        proxy_jump = profile.get("proxyJump")
        remote = upload.remote_path
        local = upload.local_path
        control_path = f"/tmp/codex-ssh-upload-{upload.id}"
        multiplex_opts = [
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=300",
            "-o",
            f"ControlPath={control_path}",
        ]

        ssh_base = ["ssh", *ssh_config_arguments(), "-p", port] + multiplex_opts
        scp_base = ["scp", *ssh_config_arguments(), "-P", port] + multiplex_opts
        if proxy_jump:
            ssh_base += ["-J", proxy_jump]
            scp_base += ["-J", proxy_jump]
        ssh = ssh_base + [user_host]
        scp = scp_base + [local, f"{user_host}:{remote}"]
        rsync_ssh = " ".join(shlex.quote(part) for part in ssh_base)
        rsync = ["rsync", "-av", "--partial", "--append-verify", "-e", rsync_ssh, local, f"{user_host}:{remote}"]

        steps = []
        remote_q = shlex.quote(remote)
        if upload.if_exists == "fail":
            steps.append(shell_join(ssh + [f"test ! -e {remote_q}"]) + " || { echo 'Remote file exists; upload aborted.'; exit 17; }")
        elif upload.if_exists == "backup":
            stamp = time.strftime("%Y%m%d%H%M%S")
            backup = shlex.quote(remote + f".bak.{stamp}")
            steps.append(shell_join(ssh + [f"if test -e {remote_q}; then mv {remote_q} {backup}; fi"]))

        steps.append(shell_join(rsync if upload.method == "rsync" else scp))
        steps.append(self._verification_command(upload, ssh, remote_q))
        cleanup = (
            shell_join(ssh_base + ["-O", "exit", user_host])
            + f" >/dev/null 2>&1 || true; rm -f {shlex.quote(control_path)}"
        )
        return ["/bin/sh", "-lc", f"trap {shlex.quote(cleanup)} EXIT; " + " && ".join(steps)]

    def _verification_command(self, upload, ssh, remote_q):
        size_cmd = (
            f"if stat -c %s {remote_q} >/dev/null 2>&1; then stat -c %s {remote_q}; "
            f"elif stat -f %z {remote_q} >/dev/null 2>&1; then stat -f %z {remote_q}; "
            f"else wc -c < {remote_q}; fi"
        )
        command = [
            "remote_size=$(" + shell_join(ssh + [size_cmd]) + ")",
            "remote_size=$(printf '%s' \"$remote_size\" | tr -d '[:space:]')",
            f"if [ \"$remote_size\" != {shlex.quote(str(upload.size))} ]; then "
            f"echo 'VERIFY_SIZE_FAIL expected={upload.size} actual='\"$remote_size\"; exit 18; "
            f"else echo 'VERIFY_SIZE_OK expected={upload.size} actual='\"$remote_size\"; fi",
        ]

        if upload.checksum == "sha256":
            expected = sha256_file(upload.local_path)
            upload.verification["sha256"] = {"expected": expected, "actual": None, "ok": None}
            hash_cmd = (
                f"if command -v sha256sum >/dev/null 2>&1; then sha256sum {remote_q} | awk '{{print $1}}'; "
                f"elif command -v shasum >/dev/null 2>&1; then shasum -a 256 {remote_q} | awk '{{print $1}}'; "
                f"else echo 'remote sha256 tool not found' >&2; exit 19; fi"
            )
            command += [
                "remote_sha=$(" + shell_join(ssh + [hash_cmd]) + ")",
                "remote_sha=$(printf '%s' \"$remote_sha\" | tr -d '[:space:]')",
                f"if [ \"$remote_sha\" != {shlex.quote(expected)} ]; then "
                f"echo 'VERIFY_SHA256_FAIL expected={expected} actual='\"$remote_sha\"; exit 20; "
                f"else echo 'VERIFY_SHA256_OK expected={expected} actual='\"$remote_sha\"; fi",
            ]

        return " && ".join(command)

    def _parse_verification(self, upload):
        text = "".join(item.get("text", "") for item in upload.log)
        size_match = re.search(r"VERIFY_SIZE_(OK|FAIL) expected=(\d+) actual=([^\s\r\n]+)", text)
        if size_match:
            actual = size_match.group(3)
            upload.verification["size"] = {
                "expected": int(size_match.group(2)),
                "actual": int(actual) if actual.isdigit() else actual,
                "ok": size_match.group(1) == "OK",
            }
        sha_match = re.search(r"VERIFY_SHA256_(OK|FAIL) expected=([0-9a-fA-F]+) actual=([^\s\r\n]+)", text)
        if sha_match:
            upload.verification["sha256"] = {
                "expected": sha_match.group(2),
                "actual": sha_match.group(3),
                "ok": sha_match.group(1) == "OK",
            }

    def _load(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
        except Exception:
            return
        for item in data.get("uploads", []):
            try:
                upload = UploadRequest(
                    None,
                    None,
                    None,
                    state=item,
                    log_limit=self.log_limit,
                    log_entry_max_chars=self.log_entry_max_chars,
                )
                self.uploads[upload.id] = upload
            except Exception:
                continue

    def _save(self):
        with self.lock:
            self._save_locked()

    def _save_locked(self):
        self._prune_locked()
        payload = {
            "version": 1,
            "retentionDays": self.retention_days,
            "pendingTtlDays": self.pending_ttl_days,
            "logLimit": self.log_limit,
            "logEntryMaxChars": self.log_entry_max_chars,
            "maxRecords": self.max_records,
            "updatedAt": now_ms(),
            "uploads": [upload.to_state() for upload in sorted(self.uploads.values(), key=lambda item: item.created_at)],
        }
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        saved_at = now_ms()
        for upload in self.uploads.values():
            upload.last_persisted_at = saved_at
        chmod_private_file(self.state_file)

    def _prune(self):
        with self.lock:
            self._prune_locked()

    def _prune_locked(self):
        current_time = now_ms()
        cutoff = current_time - self.retention_days * 24 * 60 * 60 * 1000
        pending_cutoff = current_time - self.pending_ttl_days * 24 * 60 * 60 * 1000
        for upload_id, upload in list(self.uploads.items()):
            if upload.status == "pending_confirmation" and upload.created_at < pending_cutoff:
                upload.status = "expired"
                upload.ended_at = current_time
                upload.updated_at = current_time
                upload.error = "upload request expired before user confirmation"
                upload.append_log("Upload request expired before user confirmation.\r\n")

            anchor = upload.ended_at or upload.updated_at or upload.created_at
            if upload.status not in ("pending_confirmation", "running") and anchor < cutoff:
                self.uploads.pop(upload_id, None)
                continue
            upload.log = deque(list(upload.log)[-self.log_limit :], maxlen=self.log_limit)

        if len(self.uploads) > self.max_records:
            removable = [
                upload
                for upload in self.uploads.values()
                if upload.status not in ("pending_confirmation", "running")
            ]
            removable.sort(key=lambda item: item.ended_at or item.updated_at or item.created_at)
            for upload in removable[: max(0, len(self.uploads) - self.max_records)]:
                self.uploads.pop(upload.id, None)
