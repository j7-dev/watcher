---
description: Stop the watcher daemon (kill the WezTerm pane hosting watcher.py)
---

Stop the watcher daemon: tell WezTerm to kill the pane that is hosting
`uv run watcher.py`. The watcher process exits cleanly through Python's
asyncio finally block and removes `~/.watcher/socket-info.json` and the
daemon-state file on its way out.

Run exactly:

```powershell
pwsh -NoProfile -File "${CLAUDE_PLUGIN_ROOT}/scripts/watcher-daemon.ps1" stop
```

After running:

- Exit code 0 in all normal cases — including when nothing was running
  (the script prints `not running`). Report stdout verbatim.
- Exit code non-zero: print the stderr output and stop.

Scope: process identification is restricted to the pane recorded in
`$env:USERPROFILE\.watcher\daemon-state.json`. A foreground
`uv run watcher.py` launched manually from another WezTerm pane will **not**
be detected or killed — that is intentional, so an autonomous Claude
invocation of this command cannot kill a developer's foreground instance.
If the user asks why a foreground watcher kept running after `stop`,
explain this.
