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
# `uv run watcher.py` from the repo dir. Process detection uses
# `pgrep -f watcher.py` — note that multiple watcher.py instances on the host
# will all be counted.

set -euo pipefail

SESSION="${WATCHER_TMUX_SESSION:-watcher}"

if [[ -n "${WATCHER_REPO:-}" ]]; then
  REPO="$WATCHER_REPO"
else
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

cmd="${1:-status}"

pids() {
  # `[w]atcher\.py` matches the literal string "watcher.py" but the pattern
  # itself contains "[w]atcher.py", which doesn't match — so pgrep won't
  # find its own parent shell when invoked via `bash -c` / eval wrappers.
  pgrep -f "[w]atcher\.py" || true
}

socket_path() {
  echo "${XDG_RUNTIME_DIR:-/tmp}/watcher-${USER}.sock"
}

case "$cmd" in
  status)
    p="$(pids | tr '\n' ' ')"
    if [[ -n "${p// }" ]]; then
      echo "daemon: running (pid ${p% })"
    else
      echo "daemon: not running"
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "tmux:   session '$SESSION' present (attach: tmux attach -t $SESSION)"
    else
      echo "tmux:   no '$SESSION' session"
    fi
    sock="$(socket_path)"
    if [[ -S "$sock" ]]; then
      echo "socket: $sock"
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
      remaining="$(pids | tr '\n' ' ')"
      pretty_p="$(echo "$p" | tr '\n' ' ')"
      if [[ -n "${remaining// }" ]]; then
        for pid in $remaining; do
          kill -KILL "$pid" 2>/dev/null || true
        done
        echo "force-killed pid(s): ${remaining% }"
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
