#!/usr/bin/env python3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profile_secrets import (  # noqa: E402
    SecretBackendUnavailable,
    SecretNotFound,
    delete_profile_secret,
    load_profile_secret,
    store_profile_secret,
)


def main():
    payload = {
        "name": f"smoke-{int(time.time())}",
        "mode": "ssh",
        "sshHost": "example.invalid",
        "sshPort": 22,
        "sshUser": "codex-smoke",
        "description": "temporary keychain smoke test profile",
    }
    ref = None
    saved_ref = None
    try:
        saved_ref = store_profile_secret(payload)
        ref = saved_ref
        restored = load_profile_secret(saved_ref)
        if restored != payload:
            raise RuntimeError("restored profile secret did not match the stored payload")
        delete_profile_secret(saved_ref)
        ref = None
        try:
            load_profile_secret(saved_ref)
        except SecretNotFound:
            pass
        else:
            raise RuntimeError("profile secret still exists after delete")
    except SecretBackendUnavailable as exc:
        print(f"SKIP: {exc}", file=sys.stderr)
        return 2
    finally:
        if ref:
            delete_profile_secret(ref)
    print(f"OK: system keychain backend stored, restored, and deleted {saved_ref['account']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
