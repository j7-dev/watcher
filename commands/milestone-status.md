---
description: Show the milestone-runner toggle state for every tmux pane on record
---

Print the contents of the per-pane milestone toggle state file. Useful for
checking which panes are still set to auto-rerun and for confirming that the
watcher daemon socket is actually reachable.

Run exactly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/milestone-toggle.py" status
```

After running:

- Exit code 0: report the table verbatim. The `*` column marks the current
  pane (when invoked from inside tmux). `daemon socket: unreachable` means
  the daemon is not running — no Stop event will be acted on until it is.
- Exit code non-zero: rare; print stderr and stop.
