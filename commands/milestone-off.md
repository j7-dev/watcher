---
description: Turn off the milestone-runner auto-rerun for this pane
---

Disable the per-pane milestone re-injector for the current tmux pane.
Subsequent Stop events on this pane will fall through to the daemon's normal
codex auto-response flow.

Run exactly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/milestone-toggle.py" off
```

After running:

- Exit code 0: report the printed message verbatim. `already off` is normal if
  the user ran this twice or the toggle had already auto-disabled.
- Exit code non-zero: print stderr and stop. Most likely cause: `$TMUX_PANE`
  unset (not in tmux).
