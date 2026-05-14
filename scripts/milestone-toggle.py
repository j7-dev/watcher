"""milestone-runner toggle CLI (WezTerm + Windows).

`on` / `off` flip the per-pane switch that tells the watcher daemon to inject
`/milestone-runner` into the current WezTerm pane on every Stop hook event.
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


# ---------- state file paths --------------------------------------------------

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


# ---------- TCP socket discovery ---------------------------------------------

def socket_info_path() -> Path:
    return Path.home() / ".watcher" / "socket-info.json"


def resolve_daemon_endpoint() -> tuple[str, int]:
    host = os.environ.get("WATCHER_SOCKET_HOST", "").strip() or "127.0.0.1"
    port_env = os.environ.get("WATCHER_SOCKET_PORT", "").strip()
    if port_env:
        try:
            return host, int(port_env)
        except ValueError:
            pass
    info = socket_info_path()
    if info.exists():
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
            return str(data.get("host", host)), int(data.get("port", 47823))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass
    return host, 47823


# ---------- pane discovery (WezTerm) -----------------------------------------

def require_pane() -> int:
    pane = os.environ.get("WEZTERM_PANE", "").strip()
    if not pane:
        sys.stderr.write("error: not in WezTerm ($WEZTERM_PANE unset)\n")
        sys.exit(2)
    try:
        return int(pane)
    except ValueError:
        sys.stderr.write(f"error: $WEZTERM_PANE not an integer: {pane!r}\n")
        sys.exit(2)


def window_tab_for_pane(pane_id: int) -> str:
    """Locate which window/tab a pane lives in via `wezterm cli list`.
    Used as the milestone-toggle `session_window` value so the daemon can
    auto-disable the toggle when a pane is moved between windows/tabs.
    Returns "" if pane not found or wezterm cli unavailable.
    """
    try:
        raw = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True, text=True, check=True, timeout=3.0,
        ).stdout
        data = json.loads(raw)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, json.JSONDecodeError):
        return ""
    if not isinstance(data, list):
        return ""
    for entry in data:
        try:
            if int(entry.get("pane_id", -1)) == pane_id:
                return f"{int(entry.get('window_id', 0))}:{int(entry.get('tab_id', 0))}"
        except (TypeError, ValueError):
            continue
    return ""


# ---------- state read/write -------------------------------------------------

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


# ---------- daemon liveness probe --------------------------------------------

def daemon_alive() -> bool:
    """Cheap TCP probe: connect + close. Daemon's TCP handler tolerates an
    immediate disconnect — we just want to confirm the port is listening.
    """
    host, port = resolve_daemon_endpoint()
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (OSError, socket.timeout):
        return False


# ---------- subcommands ------------------------------------------------------

def cmd_on(args: argparse.Namespace) -> int:
    pane_id = require_pane()
    key = str(pane_id)  # JSON object keys must be strings
    data = read_state()
    entry = data["panes"].get(key, {})
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    entry.update({
        "enabled": True,
        "enabled_at": now,
        "session_window": window_tab_for_pane(pane_id),
        "milestone_label": args.milestone or entry.get("milestone_label") or "",
        "reinjects": [],
        "completed_at": None,
    })
    data["panes"][key] = entry
    write_state(data)
    print(f"enabled for pane={pane_id}; will inject /milestone-runner on next Stop")
    if not daemon_alive():
        host, port = resolve_daemon_endpoint()
        print(
            f"warning: watcher daemon not reachable at {host}:{port}. "
            f"Start it with: pwsh scripts\\watcher-daemon.ps1 start",
            file=sys.stderr,
        )
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    pane_id = require_pane()
    key = str(pane_id)
    data = read_state()
    entry = data["panes"].get(key)
    if not entry or not entry.get("enabled"):
        print(f"already off for pane={pane_id}")
        return 0
    entry["enabled"] = False
    entry["disabled_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    write_state(data)
    print(f"disabled for pane={pane_id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    data = read_state()
    panes = data.get("panes") or {}
    if not panes:
        print("no panes enabled")
        return 0
    cur = os.environ.get("WEZTERM_PANE", "").strip()
    host, port = resolve_daemon_endpoint()
    print(f"state file: {state_path()}")
    print(f"daemon: {'alive' if daemon_alive() else 'unreachable'} ({host}:{port})")
    print()
    for pane_key, entry in sorted(panes.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        marker = "*" if pane_key == cur else " "
        flag = "ON " if entry.get("enabled") else "off"
        sw = entry.get("session_window") or "?"
        ml = entry.get("milestone_label") or "-"
        ts = entry.get("enabled_at") or "-"
        n = len(entry.get("reinjects") or [])
        print(f"{marker} pane={pane_key:<5} {flag}  window={sw:<8} milestone={ml:<8} "
              f"enabled_at={ts}  reinjects={n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Toggle the watcher milestone-runner re-injector for the current WezTerm pane")
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
