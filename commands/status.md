---
description: Show watcher daemon status (running pane, TCP socket endpoint)
---

Run the daemon-control script in `status` mode and report its output verbatim
so the user can see whether the daemon is alive.

Run exactly:

```powershell
pwsh -NoProfile -File "${CLAUDE_PLUGIN_ROOT}/scripts/watcher-daemon.ps1" status
```

After running:

- Exit code 0 means the daemon is running; exit code 1 means it is not
  running. Either is a normal result — do **not** treat exit code 1 as an
  error or attempt to "fix" it.
- Exit code ≥ 2 indicates a real failure (missing WezTerm, bad repo path,
  etc.); surface the stderr output to the user and stop.
- The reported `repo:` line comes from `$env:WATCHER_REPO` if set, otherwise
  the parent directory of the script (the plugin cache dir when invoked
  through `${CLAUDE_PLUGIN_ROOT}`). If the user runs the daemon from a
  separate clone, they should set `WATCHER_REPO` before invoking start/stop.
