---
description: Start the watcher daemon in a detached tmux session
---

Launch the watcher daemon via `uv run watcher.py` inside a detached tmux
session named `watcher`. The script is idempotent — re-running while the
daemon is already alive is a safe no-op.

Run exactly:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/watcher-daemon.sh" start
```

After running:

- Exit code 0: report the start message verbatim (pid, tmux session, repo).
  Remind the user they can attach with `tmux attach -t watcher` to see live
  logs.
- Exit code non-zero: print the stderr output and stop. Common causes are
  `tmux` / `uv` not installed, or `watcher.py` not found in the resolved
  repo directory. Do **not** retry automatically.

Repo resolution: by default the script runs the copy of `watcher.py` sitting
next to itself — which is the plugin cache when invoked through
`${CLAUDE_PLUGIN_ROOT}`. That works but the path changes on every plugin
update. For a stable daemon, the user should set `WATCHER_REPO` to their
permanent clone (e.g. `export WATCHER_REPO=~/DEV/watcher`) before invoking
this command. Mention this only if the resolved `repo:` looks like a plugin
cache path (`~/.claude/plugins/cache/...`).
