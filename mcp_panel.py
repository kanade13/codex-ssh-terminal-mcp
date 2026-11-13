"""MCP Apps presentation; SSH credentials remain in the local bridge."""
import sys
from pathlib import Path
from urllib.parse import urlparse

PANEL_VERSION = "0.9.1"
PANEL_URI = "ui://codex-ssh-terminal/panel-v11.html"
PANEL_MIME = "text/html;profile=mcp-app"


def bridge_origin(base_url):
    value = urlparse(base_url)
    if (
        value.scheme != "http"
        or value.hostname != "127.0.0.1"
        or value.username
        or value.password
        or value.query
        or value.fragment
        or value.path not in ("", "/")
        or not value.port
    ):
        raise ValueError("The SSH panel requires an HTTP bridge on 127.0.0.1 with an explicit port.")
    return f"http://127.0.0.1:{value.port}"


def panel_tools():
    return [
        {
            "name": "open_ssh_panel",
            "title": "SSH 终端",
            "description": (
                "Open the SSH terminal inside Codex. Starts or reuses the local bridge, "
                "without connecting to a server until the user clicks Connect. "
                "Passwords and 2FA must only be entered in the terminal's private input."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "_meta": {
                "ui": {"resourceUri": PANEL_URI},
                "openai/ui": {
                    "entrypoints": [{"type": "global"}, {"type": "thread"}],
                    "preferredModelDisplayMode": "fullscreen",
                },
            },
        },
        {
            "name": "panel_bootstrap",
            "description": "Initialize the SSH panel's local connection. For the app UI only.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "_meta": {"ui": {"visibility": ["app"]}},
        },
        {
            "name": "panel_request",
            "description": "Read terminal state or perform a user action from the SSH panel. App UI only; never send private input here.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST"]},
                    "path": {"type": "string"},
                    "payload": {"type": "object"},
                },
                "required": ["method", "path"],
                "additionalProperties": False,
            },
            "_meta": {"ui": {"visibility": ["app"]}},
        },
        {
            "name": "panel_private_input",
            "description": "Handle encrypted terminal and user-triggered clipboard requests from the SSH panel. App UI only; private text never appears in plaintext tool messages.",
            "inputSchema": {
                "type": "object",
                "properties": {"envelope": {"type": "object"}},
                "required": ["envelope"],
                "additionalProperties": False,
            },
            "_meta": {"ui": {"visibility": ["app"]}},
        },
    ]


def resource_descriptor():
    return {"uri": PANEL_URI, "name": "SSH 终端", "mimeType": PANEL_MIME}


def read_panel_resource(base_url):
    bridge_origin(base_url)
    roots = [
        Path(__file__).resolve().parent / "static",
        Path(sys.prefix) / "codex-ssh-terminal-mcp" / "static",
    ]
    static = next((root for root in roots if (root / "native-panel.js").is_file()), None)
    if static is None:
        raise FileNotFoundError("SSH panel assets are missing. Reinstall the plugin.")
    html = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "native-panel.js").read_text(encoding="utf-8")
    html = html.replace("<script>", "<script>\n" + script + "\n</script>\n<script>", 1)
    return {
        **resource_descriptor(),
        "text": html,
        "_meta": {
            "ui": {
                "prefersBorder": False,
                # Each MCP process gets an ephemeral port. The resource is
                # cached across process restarts, so pin the loopback host but
                # allow its port to change; the capability stays in UI metadata.
                "csp": {"connectDomains": ["ws://127.0.0.1:*"], "resourceDomains": []},
                "permissions": {"clipboardWrite": {}},
            },
            "openai/ui": {
                "availableDisplayModes": ["inline", "fullscreen"],
                "preferredDisplayMode": "fullscreen",
            },
        },
    }
