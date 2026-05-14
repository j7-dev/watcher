---
description: Show the milestone-runner toggle state for every WezTerm pane on record
---

Print the contents of the per-pane milestone toggle state file. Useful for
checking which panes are still set to auto-rerun and for confirming that the
watcher daemon TCP endpoint is actually reachable.

Run exactly:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/scripts/milestone-toggle.py" status
```

After running:

- Exit code 0: report the table verbatim. The `*` column marks the current
  pane (when invoked from inside WezTerm). `daemon: unreachable` means the
  daemon is not running — no Stop event will be acted on until it is.
- Exit code non-zero: rare; print stderr and stop.
