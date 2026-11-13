import json
import time

import profiles
from transfers import TransferManager, UploadRequest


def configure_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_FILE", tmp_path / "profiles.json")
    profiles.save_profile_payload(
        "prod",
        ssh_host="app.internal",
        ssh_port=22,
        ssh_user="deploy",
        proxy_jump="alice@bastion.example.com:22",
        make_default=True,
        storage_mode="plain",
    )


def test_upload_persists_compact_logs_and_verification(tmp_path, monkeypatch):
    configure_profile(tmp_path, monkeypatch)
    local = tmp_path / "artifact.txt"
    local.write_text("hello\n")
    manager = TransferManager(state_file=tmp_path / "uploads.json", retention_days=14, log_limit=3)

    public = manager.create_upload(str(local), "prod", "/tmp/artifact.txt", checksum="sha256")
    upload = manager.uploads[public["id"]]
    command = manager._build_command(upload)[2]

    assert "scp" in command
    assert "-J" in command
    assert "ControlMaster=auto" in command
    assert "ControlPersist=300" in command
    assert f"ControlPath=/tmp/codex-ssh-upload-{upload.id}" in command
    assert "-O exit" in command
    assert "VERIFY_SIZE_OK" in command
    assert "VERIFY_SHA256_OK" in command

    expected_sha = upload.verification["sha256"]["expected"]
    upload.append_log("progress 1\r")
    upload.append_log("progress 2\r")
    upload.append_log(f"VERIFY_SIZE_OK expected=6 actual=6\nVERIFY_SHA256_OK expected={expected_sha} actual={expected_sha}\n")
    manager._parse_verification(upload)
    manager._save()

    loaded = TransferManager(state_file=tmp_path / "uploads.json", retention_days=14, log_limit=3).get_upload(public["id"])
    assert loaded["verification"]["size"] == {"expected": 6, "actual": 6, "ok": True}
    assert loaded["verification"]["sha256"]["ok"] is True
    assert len(loaded["log"]) <= 3
    assert [entry for entry in loaded["log"] if entry.get("progress")][-1]["text"] == "progress 2\r"


def test_running_upload_becomes_interrupted_after_restart(tmp_path):
    state = {
        "id": "up_test",
        "filename": "artifact.txt",
        "size": 12,
        "profileName": "prod",
        "remotePath": "/tmp/artifact.txt",
        "status": "running",
        "createdAt": 1,
        "updatedAt": 2,
        "log": [],
    }

    upload = UploadRequest(None, None, None, state=state)

    assert upload.status == "interrupted"
    assert upload.error == "bridge process restarted while upload was running"
    assert upload.ended_at is not None


def test_retention_prunes_finished_uploads(tmp_path):
    old_ms = int((time.time() - 3 * 24 * 60 * 60) * 1000)
    state_file = tmp_path / "uploads.json"
    state_file.write_text(
        """{
  "uploads": [
    {
      "id": "up_old",
      "filename": "old.txt",
      "size": 1,
      "profileName": "prod",
      "remotePath": "/tmp/old.txt",
      "status": "completed",
      "createdAt": 1,
      "updatedAt": 1,
      "endedAt": %d,
      "log": []
    }
  ]
}
"""
        % old_ms
    )

    manager = TransferManager(state_file=state_file, retention_days=1, log_limit=20)

    assert manager.list_uploads() == []


def test_pending_upload_expires_after_ttl(tmp_path):
    old_ms = int((time.time() - 3 * 24 * 60 * 60) * 1000)
    state_file = tmp_path / "uploads.json"
    state_file.write_text(
        """{
  "uploads": [
    {
      "id": "up_pending",
      "filename": "pending.txt",
      "size": 1,
      "profileName": "prod",
      "remotePath": "/tmp/pending.txt",
      "status": "pending_confirmation",
      "createdAt": %d,
      "updatedAt": %d,
      "log": []
    }
  ]
}
"""
        % (old_ms, old_ms)
    )

    upload = TransferManager(state_file=state_file, retention_days=14, pending_ttl_days=1).get_upload("up_pending")

    assert upload["status"] == "expired"
    assert upload["error"] == "upload request expired before user confirmation"
    assert upload["endedAt"] is not None


def test_upload_log_entry_is_truncated(tmp_path, monkeypatch):
    configure_profile(tmp_path, monkeypatch)
    local = tmp_path / "artifact.txt"
    local.write_text("hello\n")
    manager = TransferManager(state_file=tmp_path / "uploads.json", log_entry_max_chars=220)

    public = manager.create_upload(str(local), "prod", "/tmp/artifact.txt")
    upload = manager.uploads[public["id"]]
    upload.append_log("x" * 500)

    entry = upload.public()["log"][-1]
    assert entry["truncated"] is True
    assert len(entry["text"]) <= 220
    assert "upload log entry truncated" in entry["text"]


def test_max_records_prunes_old_finished_uploads(tmp_path):
    now = int(time.time() * 1000)
    state_file = tmp_path / "uploads.json"
    uploads = []
    for index in range(25):
        uploads.append(
            {
                "id": f"up_{index}",
                "filename": f"{index}.txt",
                "size": 1,
                "profileName": "prod",
                "remotePath": f"/tmp/{index}.txt",
                "status": "completed",
                "createdAt": now + index,
                "updatedAt": now + index,
                "endedAt": now + index,
                "log": [],
            }
        )
    state_file.write_text('{"uploads": ' + json.dumps(uploads) + "}\n")

    manager = TransferManager(state_file=state_file, retention_days=14, max_records=20)

    ids = [item["id"] for item in manager.list_uploads()]
    assert len(ids) == 20
    assert "up_0" not in ids
    assert "up_24" in ids
