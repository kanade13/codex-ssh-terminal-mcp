#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

from state_paths import chmod_private_file, configure_state_home, state_file


ROOT = Path(__file__).resolve().parent
STATE_FILE = state_file("client-state.json", legacy_name=".client-state.json")
ENV_TOKEN = "CODEX_SSH_TERMINAL_TOKEN"


def refresh_state_files():
    global STATE_FILE
    STATE_FILE = state_file("client-state.json", legacy_name=".client-state.json")


def request_json(method, url, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if os.environ.get(ENV_TOKEN):
        headers["Authorization"] = f"Bearer {os.environ[ENV_TOKEN]}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    chmod_private_file(STATE_FILE)


def state_key(base_url):
    return base_url.rstrip("/")


def print_json(data):
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Client for the local Codex SSH terminal bridge.")
    parser.add_argument("--base-url", default="http://127.0.0.1:18766")
    parser.add_argument("--state-dir", help="Override local client cursor state directory.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    send = sub.add_parser("send")
    send.add_argument("command")

    snap = sub.add_parser("snapshot")
    snap.add_argument("--limit", type=int, default=80, help="Line limit by default, or event limit with --events.")
    snap.add_argument("--event-limit", type=int, default=1000, help="Maximum raw events to inspect when building compact lines.")
    snap.add_argument("--events", action="store_true", help="Return raw event chunks instead of compact terminal lines.")
    snap.add_argument("--full", action="store_true", help="Read recent history without using the saved cursor.")
    snap.add_argument("--peek", action="store_true", help="Read without advancing the saved cursor.")
    snap.add_argument("--no-compact", action="store_true", help="Disable carriage-return progress compaction.")
    snap.add_argument("--after", type=int, help="Read events after this explicit seq without using saved cursor.")

    sub.add_parser("clear-history")
    sub.add_parser("reset-cursor")

    sub.add_parser("status")

    args = parser.parse_args()
    if args.state_dir:
        configure_state_home(args.state_dir)
        refresh_state_files()
    base = args.base_url.rstrip("/")
    if args.cmd == "send":
        data = request_json("POST", base + "/api/agent-command", {"command": args.command})
    elif args.cmd == "snapshot":
        if args.events:
            if args.full:
                query = urllib.parse.urlencode({"limit": args.limit})
                data = request_json("GET", base + "/api/snapshot?" + query)
            else:
                state = load_state()
                key = state_key(base)
                after = args.after if args.after is not None else int(state.get(key, {}).get("lastSeq", 0))
                query = urllib.parse.urlencode({"after": after, "limit": args.limit})
                data = request_json("GET", base + "/api/events?" + query)
                if not args.peek:
                    state[key] = {"lastSeq": data.get("nextSeq", after + 1) - 1}
                    save_state(state)
        else:
            state = load_state()
            key = state_key(base)
            after = 0 if args.full else args.after if args.after is not None else int(state.get(key, {}).get("lastSeq", 0))
            query = urllib.parse.urlencode(
                {
                    "after": after,
                    "limit": args.event_limit,
                    "format": "lines",
                    "lineLimit": args.limit,
                    "compact": "0" if args.no_compact else "1",
                }
            )
            data = request_json("GET", base + "/api/events?" + query)
            if not args.full and not args.peek:
                state[key] = {"lastSeq": data.get("nextSeq", after + 1) - 1}
                save_state(state)
    elif args.cmd == "clear-history":
        data = request_json("POST", base + "/api/clear-history", {})
        state = load_state()
        state[state_key(base)] = {"lastSeq": data.get("nextSeq", 1) - 1}
        save_state(state)
    elif args.cmd == "reset-cursor":
        state = load_state()
        state.pop(state_key(base), None)
        save_state(state)
        data = {"ok": True, "message": "local cursor reset"}
    else:
        data = request_json("GET", base + "/api/status")
    print_json(data)


if __name__ == "__main__":
    main()
