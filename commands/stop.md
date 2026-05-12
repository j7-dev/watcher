---
description: Stop the watcher daemon (kill the tmux session + watcher.py processes)
---

Stop the watcher daemon: kill the `watcher` tmux session if present, then
send SIGTERM to any remaining `watcher.py` processes (SIGKILL after 1 s if
they ignore the term).

Run exactly:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/watcher-daemon.sh" stop
```

After running:

- Exit code 0 in all normal cases — including when nothing was running
  (the script prints `not running`). Report stdout verbatim.
- Exit code non-zero: print the stderr output and stop.

Note: `pgrep -f watcher.py` matches **every** process on the host whose
command line contains `watcher.py`. If the user has unrelated processes by
that name, this command will signal them too. Mention this caveat only if
the user reports unexpected casualties.
