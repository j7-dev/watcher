---
description: Show watcher daemon status (running pid, tmux session, socket)
---

Run the daemon-control script in `status` mode and report its output verbatim
so the user can see whether the daemon is alive.

Run exactly:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/watcher-daemon.sh" status
```

After running:

- Exit code 0 means the daemon is running; exit code 1 means it is not
  running. Either is a normal result — do **not** treat exit code 1 as an
  error or attempt to "fix" it.
- Exit code ≥ 2 indicates a real failure (missing tmux, bad repo path,
  etc.); surface the stderr output to the user and stop.
- The reported `repo:` line comes from `$WATCHER_REPO` if set, otherwise the
  parent directory of the script (the plugin cache dir when invoked through
  `${CLAUDE_PLUGIN_ROOT}`). If the user runs the daemon from a separate
  clone, they should set `WATCHER_REPO` before invoking start/stop.
