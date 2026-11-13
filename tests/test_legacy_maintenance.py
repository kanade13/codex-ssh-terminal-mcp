from types import SimpleNamespace

import legacy_maintenance as legacy


def setup_worker(monkeypatch):
    sessions = [{"profileName": name, "sessionId": name, "running": True,
                 "connected": True, "managedConnection": True} for name in legacy.BATCH_PROFILES]
    status = {"sessions": sessions, "features": {}, "connectionBatch": None}
    posts, manual = [], set()

    def get(path, params, timeout):
        if path == "/api/status":
            return status
        assert path == "/api/snapshot"
        return {"events": [{"type": "system", "text": "Session stopped."}] if params["sessionId"] in manual else []}

    def post(path, payload, timeout):
        assert path == "/api/start" and payload == {"connectAll": True}
        posts.append(payload)
        return {"ok": True}

    monkeypatch.setattr(legacy, "get_profile", lambda name: {"name": name, "sshHost": name})
    monkeypatch.setattr(legacy, "control_path", lambda name, command: "/private/" + name)
    monkeypatch.setattr(legacy, "master_ready", lambda *args: True)
    monkeypatch.setattr(legacy, "channel_ready", lambda *args: True)
    worker = legacy.LegacyMaintenance(SimpleNamespace(_get=get, _post=post))
    monkeypatch.setattr(worker, "_keep_master", lambda *args: None)
    worker.step()
    return worker, status, posts, manual


def test_existing_healthy_connections_are_never_restarted(monkeypatch):
    worker, status, posts, manual = setup_worker(monkeypatch)
    worker.step()
    assert not posts
    assert worker.seen_connected == set(legacy.BATCH_PROFILES)


def test_legacy_recovery_only_reuses_batch_and_never_stops_or_sends_input(monkeypatch):
    worker, status, posts, manual = setup_worker(monkeypatch)
    status["sessions"][1]["running"] = False
    worker.step()
    worker.step()  # rate-limited even if the bridge hasn't reported a new batch
    assert len(posts) == 1
    assert all(s["running"] for i, s in enumerate(status["sessions"]) if i != 1)


def test_legacy_user_disconnect_disables_automatic_batch_reconnect(monkeypatch):
    worker, status, posts, manual = setup_worker(monkeypatch)
    status["sessions"][1]["running"] = False
    manual.add(status["sessions"][1]["sessionId"])
    status["sessions"][2]["running"] = False
    worker.step()
    assert not posts and status["sessions"][1]["profileName"] in worker.disabled


def test_missing_master_never_reuses_old_otp_or_attempts_new_mfa_login(monkeypatch):
    worker, status, posts, manual = setup_worker(monkeypatch)
    status["sessions"][1]["running"] = False
    monkeypatch.setattr(legacy, "master_ready", lambda *args: False)
    worker.step()
    assert not posts and "完成认证" in worker.message


def test_new_bridge_recovery_takes_precedence(monkeypatch):
    worker, status, posts, manual = setup_worker(monkeypatch)
    status["sessions"][1]["running"] = False
    status["features"]["connectionRecovery"] = True
    worker.step()
    assert not posts and "新版后台" in worker.message


def test_existing_pending_authentication_is_left_untouched(monkeypatch):
    worker, status, posts, manual = setup_worker(monkeypatch)
    status["sessions"][1]["running"] = False
    status["sessions"][2]["authPromptPending"] = True
    worker.step()
    assert not posts
