"""Claude Code Stop hook → notify watcher daemon that this WezTerm pane just
stopped, so the daemon can immediately evaluate the pane (skipping the poll
interval and the `stable_count_required` consecutive-capture check).

Installed by `scripts/install-hook.py` into `~/.claude/settings.json`.

Resolution order for the WezTerm pane id:
    1. `$WEZTERM_PANE` env var (set by WezTerm in every pane's shell — Phase 1
       spike R3 verified Claude Code propagates this to Stop hook child
       processes on Windows).
    2. `cwd` field from the Claude Code Stop hook stdin JSON payload — used
       only as a sanity hint; without a real pane id we still no-op.

Connection target (TCP loopback, replacing the POSIX unix-socket transport):
    `$WATCHER_SOCKET_HOST`     env override, default 127.0.0.1
    `$WATCHER_SOCKET_PORT`     env override
    `~/.watcher/socket-info.json`  daemon writes its bound port here on startup
    fallback default port      47823 (matches watcher.py DEFAULTS)

The hook ALWAYS exits 0 on transport failure (daemon down, port closed,
timeout) — Stop hooks must never block a Claude Code turn.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47823
CONNECT_TIMEOUT = 0.5
SEND_TIMEOUT = 1.0


def socket_info_path() -> Path:
    return Path.home() / ".watcher" / "socket-info.json"


def resolve_endpoint() -> tuple[str, int]:
    host = os.environ.get("WATCHER_SOCKET_HOST", "").strip() or DEFAULT_HOST
    port_env = os.environ.get("WATCHER_SOCKET_PORT", "").strip()
    if port_env:
        try:
            return host, int(port_env)
        except ValueError:
            pass
    info_path = socket_info_path()
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            return str(info.get("host", host)), int(info.get("port", DEFAULT_PORT))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass
    return host, DEFAULT_PORT


def main() -> int:
    pane_raw = os.environ.get("WEZTERM_PANE", "").strip()
    if not pane_raw:
        return 0
    try:
        pane_id = int(pane_raw)
    except ValueError:
        return 0

    host, port = resolve_endpoint()
    payload: dict[str, object] = {"v": 1, "event": "stop", "pane": pane_id}
    token = os.environ.get("WATCHER_SOCKET_TOKEN", "").strip()
    if token:
        payload["token"] = token
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as s:
            s.settimeout(SEND_TIMEOUT)
            s.sendall(line)
    except (OSError, socket.timeout):
        # Daemon not running / port closed / firewall blocked / etc.
        # Stop hooks must NEVER block a Claude Code turn.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
