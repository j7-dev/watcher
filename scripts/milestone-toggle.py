#!/usr/bin/env python3
"""milestone-runner toggle CLI.

`on` / `off` flip the per-pane switch that tells the watcher daemon to inject
`/milestone-runner` into the current tmux pane on every Stop hook event.
`status` prints what is currently enabled.

State file (atomic write, tmp + rename):
    $WATCHER_MILESTONE_TOGGLE_STATE_PATH
    or ${XDG_STATE_HOME:-~/.local/state}/watcher/milestone-toggle.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


def state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    if base:
        return Path(base) / "watcher"
    return Path.home() / ".local" / "state" / "watcher"


def state_path() -> Path:
    override = os.environ.get("WATCHER_MILESTONE_TOGGLE_STATE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return state_root() / "milestone-toggle.json"


def socket_path() -> Path:
    explicit = os.environ.get("WATCHER_SOCKET_PATH", "").strip()
    if explicit:
        return Path(explicit)
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "default"
    base = runtime if runtime else "/tmp"
    return Path(f"{base}/watcher-{user}.sock")


def require_pane() -> str:
    pane = os.environ.get("TMUX_PANE", "").strip()
    if not pane:
        sys.stderr.write("error: not in tmux ($TMUX_PANE unset)\n")
        sys.exit(2)
    return pane


def session_window(pane: str) -> str:
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    return out


def detect_milestone_label() -> str:
    """Best-effort label, just for /watcher:milestone-status output. The daemon
    does not act on this — the marker file is the truth signal."""
    try:
        out = subprocess.run(
            ["gh", "repo", "view", "--json", "name"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if out.returncode != 0:
            return ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return ""


def read_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"version": 1, "panes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"warning: state file unreadable ({path}): {e}; resetting\n")
        return {"version": 1, "panes": {}}
    if not isinstance(data, dict):
        return {"version": 1, "panes": {}}
    data.setdefault("version", 1)
    if not isinstance(data.get("panes"), dict):
        data["panes"] = {}
    return data


def write_state(data: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def daemon_alive() -> bool:
    sp = socket_path()
    if not sp.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(str(sp))
        s.close()
        return True
    except OSError:
        return False


def cmd_on(args: argparse.Namespace) -> int:
    pane = require_pane()
    data = read_state()
    entry = data["panes"].get(pane, {})
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    entry.update({
        "enabled": True,
        "enabled_at": now,
        "session_window": session_window(pane),
        "milestone_label": args.milestone or entry.get("milestone_label") or "",
        "reinjects": [],
        "completed_at": None,
    })
    data["panes"][pane] = entry
    write_state(data)
    print(f"enabled for {pane}; will inject /milestone-runner on next Stop")
    if not daemon_alive():
        print(f"warning: watcher daemon socket not reachable ({socket_path()}). "
              f"Start it with: bash scripts/watcher-daemon.sh start", file=sys.stderr)
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    pane = require_pane()
    data = read_state()
    entry = data["panes"].get(pane)
    if not entry or not entry.get("enabled"):
        print(f"already off for {pane}")
        return 0
    entry["enabled"] = False
    entry["disabled_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    write_state(data)
    print(f"disabled for {pane}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    data = read_state()
    panes = data.get("panes") or {}
    if not panes:
        print("no panes enabled")
        return 0
    cur = os.environ.get("TMUX_PANE", "").strip()
    print(f"state file: {state_path()}")
    print(f"daemon socket: {'alive' if daemon_alive() else 'unreachable'} ({socket_path()})")
    print()
    for pane_id, entry in sorted(panes.items()):
        marker = "*" if pane_id == cur else " "
        flag = "ON " if entry.get("enabled") else "off"
        sw = entry.get("session_window") or "?"
        ml = entry.get("milestone_label") or "-"
        ts = entry.get("enabled_at") or "-"
        n = len(entry.get("reinjects") or [])
        print(f"{marker} {pane_id:<6} {flag}  window={sw:<14} milestone={ml:<8} "
              f"enabled_at={ts}  reinjects={n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Toggle the watcher milestone-runner re-injector for the current tmux pane")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_on = sub.add_parser("on", help="enable for current pane")
    p_on.add_argument("--milestone", default="", help="optional label (cosmetic)")
    p_on.set_defaults(func=cmd_on)
    p_off = sub.add_parser("off", help="disable for current pane")
    p_off.set_defaults(func=cmd_off)
    p_st = sub.add_parser("status", help="show all per-pane state")
    p_st.set_defaults(func=cmd_status)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
