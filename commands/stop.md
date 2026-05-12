---
description: Stop the watcher daemon (kill the tmux session + watcher.py processes)
---

Stop the watcher daemon: kill the `watcher` tmux session if present, then
send SIGTERM to any remaining `watcher.py` processes inside that tmux
session (SIGKILL after 1 s if they ignore the term).

Run exactly:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/watcher-daemon.sh" stop
```

After running:

- Exit code 0 in all normal cases — including when nothing was running
  (the script prints `not running`). Report stdout verbatim.
- Exit code non-zero: print the stderr output and stop.

Scope: process detection is restricted to the Unix session id (SID) of the
`watcher` tmux pane. A foreground `uv run watcher.py` launched from the
user's own shell (outside this tmux session) will **not** be detected or
signalled — that is intentional, so an autonomous Claude invocation of
this command cannot kill a developer's foreground instance. If the user
asks why a foreground watcher kept running after `stop`, explain this.
