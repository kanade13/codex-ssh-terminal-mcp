import os
import shutil
import stat
import sys
from pathlib import Path


APP_NAME = "codex-ssh-terminal-mcp"
ENV_HOME = "CODEX_SSH_TERMINAL_HOME"
ENV_STORAGE = "CODEX_SSH_TERMINAL_STORAGE"
SOURCE_ROOT = Path(__file__).resolve().parent
PROJECT_STATE_DIRNAME = ".codex-ssh-terminal"

_override_home = None


def configure_state_home(path=None, storage=None):
    global _override_home
    if path:
        _override_home = Path(path).expanduser().resolve()
        os.environ[ENV_HOME] = str(_override_home)
    if storage:
        storage = str(storage).strip().lower()
        if storage not in ("project", "user", "auto"):
            raise ValueError("storage must be one of: project, user, auto")
        os.environ[ENV_STORAGE] = storage


def source_root():
    return SOURCE_ROOT


def is_source_checkout():
    root = source_root()
    return (root / ".git").exists() and (root / "pyproject.toml").exists()


def storage_mode():
    value = os.environ.get(ENV_STORAGE, "").strip().lower()
    if value in ("project", "user"):
        return value
    if value and value != "auto":
        raise ValueError(f"{ENV_STORAGE} must be one of: project, user, auto")
    return "project" if is_source_checkout() else "user"


def user_state_dir():
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(base) / APP_NAME


def project_state_dir():
    return source_root() / PROJECT_STATE_DIRNAME


def state_dir():
    if _override_home:
        return ensure_private_dir(_override_home)
    if os.environ.get(ENV_HOME):
        return ensure_private_dir(Path(os.environ[ENV_HOME]).expanduser().resolve())
    if storage_mode() == "project":
        return ensure_private_dir(project_state_dir())
    return ensure_private_dir(user_state_dir())


def state_file(filename, legacy_name=None):
    target = state_dir() / filename
    if legacy_name and storage_mode() == "project":
        legacy = source_root() / legacy_name
        if legacy.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(legacy), str(target))
            except OSError:
                shutil.copy2(legacy, target)
    return target


def ensure_private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def chmod_private_file(path):
    try:
        Path(path).chmod(0o600)
    except OSError:
        pass


def is_private_dir(path):
    if os.name == "nt":
        return True
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    return mode & 0o077 == 0
