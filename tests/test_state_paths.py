from pathlib import Path

import state_paths


def reset_state_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(state_paths, "_override_home", None)
    monkeypatch.setattr(state_paths, "SOURCE_ROOT", tmp_path / "src")
    monkeypatch.delenv(state_paths.ENV_HOME, raising=False)
    monkeypatch.delenv(state_paths.ENV_STORAGE, raising=False)


def test_source_checkout_defaults_to_project_state_dir(tmp_path, monkeypatch):
    reset_state_paths(monkeypatch, tmp_path)
    root = state_paths.SOURCE_ROOT
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")

    path = state_paths.state_file("mcp-state.json")

    assert path == root / ".codex-ssh-terminal" / "mcp-state.json"
    assert path.parent.exists()
    assert state_paths.is_private_dir(path.parent)


def test_packaged_mode_defaults_to_user_config_dir(tmp_path, monkeypatch):
    reset_state_paths(monkeypatch, tmp_path)
    state_paths.SOURCE_ROOT.mkdir()
    monkeypatch.setattr(state_paths.sys, "platform", "linux")
    monkeypatch.setattr(state_paths.os, "name", "posix")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    path = state_paths.state_file("mcp-state.json")

    assert path == tmp_path / "xdg-config" / "codex-ssh-terminal-mcp" / "mcp-state.json"
    assert path.parent.exists()
    assert state_paths.is_private_dir(path.parent)


def test_explicit_state_home_wins(tmp_path, monkeypatch):
    reset_state_paths(monkeypatch, tmp_path)
    root = state_paths.SOURCE_ROOT
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    explicit = tmp_path / "custom-state"

    state_paths.configure_state_home(explicit)

    assert state_paths.state_file("runtime-config.json") == explicit / "runtime-config.json"


def test_legacy_project_file_is_migrated(tmp_path, monkeypatch):
    reset_state_paths(monkeypatch, tmp_path)
    root = state_paths.SOURCE_ROOT
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    legacy = root / ".mcp-state.json"
    legacy.write_text("{}\n")

    path = state_paths.state_file("mcp-state.json", legacy_name=".mcp-state.json")

    assert path == root / ".codex-ssh-terminal" / "mcp-state.json"
    assert path.read_text() == "{}\n"
    assert not legacy.exists()
