import pytest

import profiles


def test_wsl_default_and_dedicated_ssh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_FILE", tmp_path / "profiles.json")
    monkeypatch.setenv("CODEX_SSH_CONFIG", "/tmp/private-state/ssh_config")

    def no_keychain(*args, **kwargs):
        raise AssertionError("The WSL default must work without macOS Keychain")

    monkeypatch.setattr(profiles, "store_profile_secret", no_keychain)
    _, profile = profiles.save_profile_payload("gpu", ssh_host="gpu.example", ssh_user="alice")
    assert profile["storageMode"] == "plain"
    assert profiles.profile_to_command(profile) == [
        "ssh", "-F", "/tmp/private-state/ssh_config", "-p", "22", "alice@gpu.example",
    ]


def fake_keychain(monkeypatch):
    secrets = {}

    def store(payload, ref=None):
        ref = ref or {"backend": "fake", "service": "test", "account": f"profile:{len(secrets) + 1}"}
        secrets[ref["account"]] = payload
        return ref

    def load(ref):
        return secrets[ref["account"]]

    def delete(ref):
        if ref:
            secrets.pop(ref["account"], None)

    monkeypatch.setattr(profiles, "store_profile_secret", store)
    monkeypatch.setattr(profiles, "load_profile_secret", load)
    monkeypatch.setattr(profiles, "delete_profile_secret", delete)
    return secrets


def test_profile_save_redacted_and_proxyjump_command(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_FILE", tmp_path / "profiles.json")
    fake_keychain(monkeypatch)

    data, profile = profiles.save_profile_payload(
        "prod",
        ssh_host="app.internal",
        ssh_port=2222,
        ssh_user="deploy",
        proxy_jump="alice@bastion.example.com:22",
        description="production",
        make_default=True,
        storage_mode="keychain",
    )

    assert data["defaultProfile"] == "prod"
    assert profile["proxyJump"] == "alice@bastion.example.com:22"
    stored = profiles.load_profiles()["profiles"]["prod"]
    assert stored["storageMode"] == "keychain"
    assert stored["secretRef"]["backend"] == "fake"
    assert "sshHost" not in stored
    assert "sshUser" not in stored
    assert "proxyJump" not in stored
    assert profiles.profile_to_command(profile) == [
        "ssh",
        "-p",
        "2222",
        "-J",
        "alice@bastion.example.com:22",
        "deploy@app.internal",
    ]

    redacted = profiles.redacted_profiles()
    assert redacted["profiles"] == [
        {
            "name": "prod",
            "mode": "ssh",
            "storageMode": "keychain",
            "public": {
                "hostHint": "ap***.internal",
                "userHint": "de***",
                "portHint": 2222,
                "proxyJumpHint": "al***",
            },
            "description": "production",
            "isDefault": True,
            "createdAt": profile["createdAt"],
            "updatedAt": profile["updatedAt"],
        }
    ]
    assert "sshHost" not in redacted["profiles"][0]
    assert "sshUser" not in redacted["profiles"][0]
    assert "proxyJump" not in redacted["profiles"][0]


def test_command_profile_is_split_for_terminal_only(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_FILE", tmp_path / "profiles.json")

    _, profile = profiles.save_profile_payload("menu", command="ssh -p 2222 user@bastion", storage_mode="plain")

    assert profile["mode"] == "command"
    assert profiles.profile_to_command(profile) == ["ssh", "-p", "2222", "user@bastion"]


def test_plain_profile_redaction_gets_public_hints(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_FILE", tmp_path / "profiles.json")

    profiles.save_profile_payload(
        "legacy",
        ssh_host="legacy.internal",
        ssh_user="deploy",
        ssh_port=2200,
        storage_mode="plain",
    )

    redacted = profiles.redacted_profiles()["profiles"][0]

    assert redacted["storageMode"] == "plain"
    assert redacted["public"] == {
        "hostHint": "le***.internal",
        "userHint": "de***",
        "portHint": 2200,
        "proxyJumpHint": "",
    }
    assert "sshHost" not in redacted
    assert "sshUser" not in redacted


def test_delete_keychain_profile_removes_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_FILE", tmp_path / "profiles.json")
    secrets = fake_keychain(monkeypatch)

    profiles.save_profile_payload("prod", ssh_host="app.internal", storage_mode="keychain")
    assert secrets

    _, existed = profiles.delete_profile_payload("prod")

    assert existed is True
    assert secrets == {}


def test_failed_keychain_save_deletes_new_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_FILE", tmp_path / "profiles.json")
    secrets = fake_keychain(monkeypatch)

    def fail_save(data):
        raise OSError("disk full")

    monkeypatch.setattr(profiles, "save_profiles", fail_save)

    with pytest.raises(OSError, match="disk full"):
        profiles.save_profile_payload("prod", ssh_host="app.internal", storage_mode="keychain")

    assert secrets == {}


def test_failed_keychain_update_restores_previous_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILE_FILE", tmp_path / "profiles.json")
    secrets = fake_keychain(monkeypatch)

    profiles.save_profile_payload("prod", ssh_host="old.internal", storage_mode="keychain")
    stored = profiles.load_profiles()["profiles"]["prod"]
    ref = stored["secretRef"]
    assert secrets[ref["account"]]["sshHost"] == "old.internal"

    def fail_save(data):
        raise OSError("disk full")

    monkeypatch.setattr(profiles, "save_profiles", fail_save)

    with pytest.raises(OSError, match="disk full"):
        profiles.save_profile_payload("prod", ssh_host="new.internal", storage_mode="keychain")

    assert secrets[ref["account"]]["sshHost"] == "old.internal"
