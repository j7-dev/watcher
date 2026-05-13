#!/usr/bin/env python3
"""Claude Code Stop hook → notify watcher daemon that this tmux pane just stopped.

Install via `~/.claude/settings.json`:

    {
      "hooks": {
        "Stop": [
          {
            "matcher": "*",
            "hooks": [
              { "type": "command",
                "command": "/home/j7/DEV/watcher/hooks/claude-stop-notify.py" }
            ]
          }
        ]
      }
    }

The script reads `$TMUX_PANE` (set by tmux for every process running inside a
pane) and pushes that pane id to the watcher's unix domain socket. If the
watcher isn't running, or we're not inside tmux, the hook silently no-ops so
it never blocks the Claude Code turn.
"""
from __future__ import annotations

import json
import os
import socket
import sys


def default_socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "default"
    base = runtime if runtime else "/tmp"
    return f"{base}/watcher-{user}.sock"


def main() -> int:
    pane = os.environ.get("TMUX_PANE", "").strip()
    if not pane:
        return 0
    sock_path = os.environ.get("WATCHER_SOCKET_PATH", "").strip() or default_socket_path()
    if not os.path.exists(sock_path):
        return 0
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(sock_path)
        payload = json.dumps({"v": 1, "event": "stop", "pane": pane}, ensure_ascii=False)
        s.sendall((payload + "\n").encode("utf-8"))
        s.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
