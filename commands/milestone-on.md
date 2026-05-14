---
description: Turn on auto-rerun of /milestone-runner on every Stop in this pane
---

Enable the watcher daemon's per-pane milestone re-injector for the current
WezTerm pane. From now on, every time this Claude Code agent finishes a turn
(Stop hook fires), the daemon will inject `/milestone-runner` back into this
pane until the SKILL writes its completion marker, the user runs
`/watcher:milestone-off`, or the killswitch trips after repeated injects
without progress.

Run exactly:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/scripts/milestone-toggle.py" on
```

After running:

- Exit code 0: report the printed message verbatim. If a stderr warning about
  the daemon socket appears, surface it — the toggle is recorded but nothing
  will inject until the daemon is started
  (`pwsh scripts\watcher-daemon.ps1 start`).
- Exit code non-zero: print stderr and stop. The most common cause is running
  outside of WezTerm (`$WEZTERM_PANE` unset).
