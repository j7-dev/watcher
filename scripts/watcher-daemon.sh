#!/usr/bin/env bash
# Start / stop / status of the watcher daemon.
#
# Usage:
#   watcher-daemon.sh status
#   watcher-daemon.sh start
#   watcher-daemon.sh stop
#
# Repo resolution (where `watcher.py` lives):
#   1. $WATCHER_REPO if set
#   2. parent dir of this script (works when invoked from a clone OR the
#      plugin cache, since both layouts ship watcher.py at the repo root)
#
# Daemon lifecycle: a detached tmux session named "$SESSION" runs
# `uv run watcher.py` from the repo dir. Process detection is scoped to the
# Unix session id (SID) of the tmux pane — so foreground `uv run watcher.py`
# instances launched outside this tmux session are NOT detected, started,
# or signalled. This is intentional: prevents another Claude Code session
# from `bash watcher-daemon.sh stop`-ing your foreground dev instance.

set -euo pipefail

SESSION="${WATCHER_TMUX_SESSION:-watcher}"

if [[ -n "${WATCHER_REPO:-}" ]]; then
  REPO="$WATCHER_REPO"
else
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

cmd="${1:-status}"

pids() {
  # Scope detection to processes inside the "$SESSION" tmux pane only.
  # We grab the pane's Unix session id (SID) and ask pgrep for watcher.py
  # processes in that SID. Processes outside the tmux session (e.g. a
  # foreground `uv run watcher.py` in your own shell) live in a different
  # SID and are deliberately ignored.
  #
  # `[w]atcher\.py` matches the literal string "watcher.py" but the pattern
  # itself contains "[w]atcher.py", which doesn't match — so pgrep won't
  # find its own parent shell when invoked via `bash -c` / eval wrappers.
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    return 0
  fi
  local pane_pid
  pane_pid="$(tmux list-panes -t "$SESSION" -F '#{pane_pid}' 2>/dev/null | head -n1)"
  [[ -z "$pane_pid" ]] && return 0
  local sid
  sid="$(ps -o sid= -p "$pane_pid" 2>/dev/null | tr -d ' ')"
  [[ -z "$sid" ]] && return 0
  pgrep -s "$sid" -f "[w]atcher\.py" 2>/dev/null || true
}

socket_path() {
  echo "${XDG_RUNTIME_DIR:-/tmp}/watcher-${USER}.sock"
}

case "$cmd" in
  status)
    p="$(pids | tr '\n' ' ')"
    if [[ -n "${p// }" ]]; then
      echo "daemon: running in tmux session '$SESSION' (pid ${p% })"
    else
      echo "daemon: no tmux-managed daemon in session '$SESSION'"
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "tmux:   session '$SESSION' present (attach: tmux attach -t $SESSION)"
    else
      echo "tmux:   no '$SESSION' session"
    fi
    sock="$(socket_path)"
    if [[ -S "$sock" ]]; then
      echo "socket: $sock"
      if [[ -z "${p// }" ]]; then
        # Live socket but no tmux-session daemon → likely a foreground
        # `uv run watcher.py` outside this script's reach.
        echo "note:   socket is live but daemon is out-of-session (foreground?) — this script will not signal it"
      fi
    else
      echo "socket: absent ($sock)"
    fi
    echo "repo:   $REPO"
    [[ -n "$p" ]] && exit 0 || exit 1
    ;;

  start)
    existing="$(pids | tr '\n' ' ')"
    if [[ -n "${existing// }" ]]; then
      echo "already running (pid ${existing% })"
      exit 0
    fi
    # Detect an out-of-session watcher (e.g. foreground `uv run watcher.py`)
    # via a live socket. Starting a tmux daemon on top of it would silently
    # hijack the socket — refuse instead.
    sock="$(socket_path)"
    if [[ -S "$sock" ]]; then
      echo "already running outside tmux session '$SESSION' (live socket: $sock)"
      echo "this start is a no-op; stop the other instance first if you want a tmux-managed daemon"
      exit 0
    fi
    if [[ ! -f "$REPO/watcher.py" ]]; then
      echo "error: watcher.py not found in: $REPO" >&2
      echo "set WATCHER_REPO to the clone directory" >&2
      exit 2
    fi
    if ! command -v tmux >/dev/null; then
      echo "error: tmux not installed" >&2
      exit 2
    fi
    if ! command -v uv >/dev/null; then
      echo "error: uv not installed" >&2
      exit 2
    fi
    # clean stale session with the same name but no process behind it
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      tmux kill-session -t "$SESSION"
    fi
    tmux new-session -d -s "$SESSION" -c "$REPO" "uv run watcher.py"
    sleep 1
    p="$(pids | tr '\n' ' ')"
    if [[ -n "${p// }" ]]; then
      echo "started (pid ${p% })"
      echo "tmux session: $SESSION  (attach: tmux attach -t $SESSION)"
      echo "repo: $REPO"
    else
      echo "start failed; inspect 'tmux attach -t $SESSION' for errors" >&2
      exit 1
    fi
    ;;

  stop)
    p="$(pids)"
    has_session=0
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      has_session=1
    fi
    if [[ -z "$p" && $has_session -eq 0 ]]; then
      echo "not running"
      exit 0
    fi
    if [[ $has_session -eq 1 ]]; then
      tmux kill-session -t "$SESSION"
      echo "tmux session '$SESSION' killed"
    fi
    if [[ -n "$p" ]]; then
      for pid in $p; do
        kill -TERM "$pid" 2>/dev/null || true
      done
      sleep 1
      # `pids()` is now tmux-session-scoped, and we already killed the
      # session above — so we re-check the originally captured PIDs
      # individually with `kill -0` instead of calling `pids()` again.
      remaining=""
      for pid in $p; do
        if kill -0 "$pid" 2>/dev/null; then
          remaining="$remaining $pid"
        fi
      done
      remaining="${remaining# }"
      pretty_p="$(echo "$p" | tr '\n' ' ')"
      if [[ -n "$remaining" ]]; then
        for pid in $remaining; do
          kill -KILL "$pid" 2>/dev/null || true
        done
        echo "force-killed pid(s): $remaining"
      else
        echo "stopped pid(s): ${pretty_p% }"
      fi
    fi
    ;;

  *)
    echo "usage: $(basename "$0") {status|start|stop}" >&2
    exit 2
    ;;
esac
