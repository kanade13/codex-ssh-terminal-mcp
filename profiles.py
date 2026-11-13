import json
import os
import shlex
import time
from pathlib import Path

from profile_secrets import delete_profile_secret, load_profile_secret, store_profile_secret
from state_paths import chmod_private_file, state_file


ROOT = Path(__file__).resolve().parent
PROFILE_FILE = state_file("connection-profiles.json", legacy_name=".connection-profiles.json")
DEFAULT_STORAGE_MODE = "plain"


def ssh_config_arguments():
    config = os.environ.get("CODEX_SSH_CONFIG")
    return ["-F", config] if config else []


def load_profiles():
    if not PROFILE_FILE.exists():
        return {"profiles": {}, "defaultProfile": None}
    try:
        data = json.loads(PROFILE_FILE.read_text())
    except Exception:
        return {"profiles": {}, "defaultProfile": None}
    if "profiles" not in data or not isinstance(data["profiles"], dict):
        data["profiles"] = {}
    data.setdefault("defaultProfile", None)
    return data


def save_profiles(data):
    PROFILE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    chmod_private_file(PROFILE_FILE)


def cleanup_secret(ref):
    try:
        delete_profile_secret(ref)
    except Exception:
        pass


def restore_secret(ref, payload):
    if not ref or payload is None:
        return
    try:
        store_profile_secret(payload, ref)
    except Exception:
        pass


def validate_profile_payload(
    name,
    ssh_host=None,
    ssh_port=22,
    ssh_user=None,
    command=None,
    description="",
    proxy_jump=None,
):
    if not name or not str(name).strip():
        raise ValueError("profile name is required")
    profile_name = str(name).strip()
    desc = str(description).strip() if description else ""

    if command and str(command).strip():
        profile = {
            "name": profile_name,
            "mode": "command",
            "command": str(command).strip(),
        }
    else:
        if not ssh_host or not str(ssh_host).strip():
            raise ValueError("ssh_host is required when command is not provided")
        profile = {
            "name": profile_name,
            "mode": "ssh",
            "sshHost": str(ssh_host).strip(),
            "sshPort": int(ssh_port or 22),
            "sshUser": str(ssh_user).strip() if ssh_user else None,
        }
        if proxy_jump and str(proxy_jump).strip():
            profile["proxyJump"] = str(proxy_jump).strip()

    if desc:
        profile["description"] = desc
    return profile


def mask_value(value, keep=2):
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= keep:
        return value[0] + "*" * max(0, len(value) - 1)
    return value[:keep] + "***"


def mask_host(host):
    parts = str(host or "").strip().split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return ".".join(parts[:2] + ["*", "*"])
    if len(parts) > 1:
        return mask_value(parts[0]) + "." + ".".join(parts[1:])
    return mask_value(host)


def profile_public_hints(profile):
    if profile.get("mode") == "command":
        return {"commandHint": "ssh ..."}
    return {
        "hostHint": mask_host(profile.get("sshHost", "")),
        "userHint": mask_value(profile.get("sshUser", "")),
        "portHint": profile.get("sshPort", 22),
        "proxyJumpHint": mask_value(profile.get("proxyJump", "")) if profile.get("proxyJump") else "",
    }


def metadata_profile(profile, existing=None, storage_mode=DEFAULT_STORAGE_MODE):
    public = {
        "name": profile["name"],
        "mode": profile.get("mode", "ssh"),
        "storageMode": storage_mode,
        "public": profile_public_hints(profile),
    }
    if profile.get("description"):
        public["description"] = profile["description"]
    if existing and existing.get("createdAt"):
        public["createdAt"] = existing["createdAt"]
    return public


def resolve_profile(profile):
    if not profile:
        return None
    if profile.get("storageMode") != "keychain":
        return profile
    secret_ref = profile.get("secretRef")
    if not secret_ref:
        raise ValueError(f"profile is missing secretRef: {profile.get('name')}")
    secret = load_profile_secret(secret_ref)
    resolved = {
        **secret,
        "name": profile.get("name"),
        "description": profile.get("description", ""),
        "storageMode": "keychain",
        "createdAt": profile.get("createdAt"),
        "updatedAt": profile.get("updatedAt"),
    }
    return resolved


def save_profile_payload(
    name,
    ssh_host=None,
    ssh_port=22,
    ssh_user=None,
    command=None,
    description="",
    proxy_jump=None,
    make_default=False,
    storage_mode=DEFAULT_STORAGE_MODE,
):
    profile = validate_profile_payload(name, ssh_host, ssh_port, ssh_user, command, description, proxy_jump)
    storage_mode = str(storage_mode or DEFAULT_STORAGE_MODE).strip()
    if storage_mode not in ("keychain", "plain"):
        raise ValueError("storage_mode must be one of: keychain, plain")
    now = int(time.time() * 1000)

    data = load_profiles()
    existing = data["profiles"].get(profile["name"], {})
    new_secret_ref = None
    previous_secret_payload = None
    old_secret_ref_to_delete = None

    if storage_mode == "keychain":
        existing_secret_ref = existing.get("secretRef") if existing.get("storageMode") == "keychain" else None
        if existing_secret_ref:
            try:
                previous_secret_payload = load_profile_secret(existing_secret_ref)
            except Exception:
                previous_secret_payload = None
        secret_ref = store_profile_secret(profile, existing_secret_ref)
        if not existing_secret_ref:
            new_secret_ref = secret_ref
        public = metadata_profile(profile, existing, storage_mode="keychain")
        public["secretRef"] = secret_ref
        if existing.get("storageMode") not in (None, "keychain") and existing.get("secretRef"):
            old_secret_ref_to_delete = existing.get("secretRef")
        profile_to_save = public
    else:
        if existing.get("storageMode") == "keychain" and existing.get("secretRef"):
            old_secret_ref_to_delete = existing.get("secretRef")
        profile_to_save = dict(profile)
        profile_to_save["storageMode"] = "plain"

    profile_to_save["createdAt"] = existing.get("createdAt", now)
    profile_to_save["updatedAt"] = now
    data["profiles"][profile["name"]] = profile_to_save
    if make_default or not data.get("defaultProfile"):
        data["defaultProfile"] = profile["name"]
    try:
        save_profiles(data)
    except Exception:
        if new_secret_ref:
            cleanup_secret(new_secret_ref)
        else:
            restore_secret(existing.get("secretRef"), previous_secret_payload)
        raise
    cleanup_secret(old_secret_ref_to_delete)
    return data, resolve_profile(profile_to_save)


def delete_profile_payload(name):
    if not name or not str(name).strip():
        raise ValueError("profile name is required")
    profile_name = str(name).strip()
    data = load_profiles()
    existing = data["profiles"].pop(profile_name, None)
    existed = existing is not None
    if existing and existing.get("storageMode") == "keychain":
        delete_profile_secret(existing.get("secretRef"))
    if data.get("defaultProfile") == profile_name:
        data["defaultProfile"] = next(iter(data["profiles"]), None)
    save_profiles(data)
    return data, existed


def set_default_profile_payload(name):
    if not name or not str(name).strip():
        raise ValueError("profile name is required")
    profile_name = str(name).strip()
    data = load_profiles()
    if profile_name not in data["profiles"]:
        raise ValueError(f"profile not found: {profile_name}")
    data["defaultProfile"] = profile_name
    save_profiles(data)
    return data


def get_profile(name=None):
    data = load_profiles()
    profile_name = name or data.get("defaultProfile")
    if not profile_name:
        return None
    return resolve_profile(data["profiles"].get(profile_name))


def profile_to_command(profile):
    if not profile:
        raise ValueError("profile is required")
    if profile.get("mode") == "command":
        return shlex.split(profile["command"])
    target = profile["sshHost"]
    if profile.get("sshUser"):
        target = f"{profile['sshUser']}@{target}"
    command = ["ssh", *ssh_config_arguments(), "-p", str(profile.get("sshPort", 22))]
    if profile.get("proxyJump"):
        command += ["-J", profile["proxyJump"]]
    return command + [target]


def redacted_profile(profile, is_default=False):
    return {
        "name": profile.get("name"),
        "mode": profile.get("mode", "ssh"),
        "storageMode": profile.get("storageMode", "plain"),
        "public": profile.get("public") or profile_public_hints(profile),
        "description": profile.get("description", ""),
        "isDefault": bool(is_default),
        "createdAt": profile.get("createdAt"),
        "updatedAt": profile.get("updatedAt"),
    }


def redacted_profiles():
    data = load_profiles()
    default_name = data.get("defaultProfile")
    return {
        "profileFile": str(PROFILE_FILE),
        "defaultProfile": default_name,
        "profiles": [
            redacted_profile(profile, name == default_name)
            for name, profile in sorted(data["profiles"].items())
        ],
    }
