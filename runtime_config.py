import json
import sys
import time
from pathlib import Path

from state_paths import chmod_private_file, state_file


ROOT = Path(__file__).resolve().parent
RUNTIME_CONFIG_FILE = state_file("runtime-config.json", legacy_name=".runtime-config.json")


def now_ms():
    return int(time.time() * 1000)


def load_runtime_config():
    if not RUNTIME_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(RUNTIME_CONFIG_FILE.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_runtime_config(data):
    payload = dict(data)
    payload["updatedAt"] = now_ms()
    RUNTIME_CONFIG_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    chmod_private_file(RUNTIME_CONFIG_FILE)
    return payload


def runtime_config_status():
    data = load_runtime_config()
    configured = bool(data.get("mode"))
    redacted = {
        "configured": configured,
        "configFile": str(RUNTIME_CONFIG_FILE),
        "currentPython": sys.executable,
    }
    if configured:
        redacted.update(
            {
                "mode": data.get("mode"),
                "pythonPath": data.get("pythonPath"),
                "condaEnv": data.get("condaEnv"),
                "condaExecutable": data.get("condaExecutable"),
                "updatedAt": data.get("updatedAt"),
            }
        )
    else:
        redacted["hint"] = (
            "Runtime is not configured. Ask the user whether to run the bridge with a Python executable "
            "or a Conda environment, then call set_runtime_config."
        )
    return redacted


def set_runtime_config_payload(mode, python_path=None, conda_env=None, conda_executable=None):
    mode = str(mode or "").strip()
    if mode not in ("python", "conda", "current"):
        raise ValueError("mode must be one of: python, conda, current")

    if mode == "current":
        payload = {
            "mode": "python",
            "pythonPath": sys.executable,
        }
    elif mode == "python":
        if not python_path or not str(python_path).strip():
            raise ValueError("python_path is required when mode=python")
        payload = {
            "mode": "python",
            "pythonPath": str(python_path).strip(),
        }
    else:
        if not conda_env or not str(conda_env).strip():
            raise ValueError("conda_env is required when mode=conda")
        payload = {
            "mode": "conda",
            "condaEnv": str(conda_env).strip(),
            "condaExecutable": str(conda_executable).strip() if conda_executable else "conda",
        }
    return save_runtime_config(payload)


def bridge_python_command(require_config=True):
    data = load_runtime_config()
    mode = data.get("mode")
    if not mode:
        if require_config:
            raise RuntimeError(
                "Bridge runtime is not configured. Ask the user for either a Python executable path "
                "or a Conda environment name, then call set_runtime_config before starting the config server."
            )
        return [sys.executable]
    if mode == "python":
        python_path = data.get("pythonPath")
        if not python_path:
            raise RuntimeError("runtime config is invalid: pythonPath is missing")
        return [python_path]
    if mode == "conda":
        conda_env = data.get("condaEnv")
        if not conda_env:
            raise RuntimeError("runtime config is invalid: condaEnv is missing")
        return [data.get("condaExecutable") or "conda", "run", "-n", conda_env, "python"]
    raise RuntimeError(f"runtime config is invalid: unsupported mode {mode}")
