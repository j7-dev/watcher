---
description: Start the watcher daemon in a dedicated WezTerm window
---

Launch the watcher daemon via `uv run watcher.py` inside a dedicated WezTerm
window/pane (created with `wezterm cli spawn --new-window --workspace watcher`).
The script is idempotent — re-running while the daemon is already alive is a
safe no-op.

Run exactly:

```powershell
pwsh -NoProfile -File "${CLAUDE_PLUGIN_ROOT}/scripts/watcher-daemon.ps1" start
```

After running:

- Exit code 0: report the start message verbatim (pane_id, wezterm_pid, repo).
  Remind the user they can attach with
  `wezterm cli activate-pane --pane-id <id>` to see live logs.
- Exit code non-zero: print the stderr output and stop. Common causes are
  `wezterm` GUI not running, `uv` not installed, or `watcher.py` not found
  in the resolved repo directory. Do **not** retry automatically.

Repo resolution: by default the script runs the copy of `watcher.py` sitting
next to itself — which is the plugin cache when invoked through
`${CLAUDE_PLUGIN_ROOT}`. That works but the path changes on every plugin
update. For a stable daemon, the user should set `WATCHER_REPO` to their
permanent clone (e.g. `$env:WATCHER_REPO = "$env:USERPROFILE\DEV\watcher"`)
before invoking this command. Mention this only if the resolved `repo:` looks
like a plugin cache path (`~/.claude/plugins/cache/...`).
