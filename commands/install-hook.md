---
description: Install the watcher Stop hook into ~/.claude/settings.json (idempotent)
argument-hint: "[--uninstall|--status]"
---

You are running the watcher install-hook script. Forward `$ARGUMENTS` (which
may be `--uninstall`, `--status`, or empty) to the script and report its
output verbatim so the user can see whether the install succeeded.

Run exactly:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/scripts/install-hook.py" $ARGUMENTS
```

After running:

- If exit code is 0, summarize the result in one line (e.g. "Hook installed at
  `<path>`" or "Already installed" or "Uninstalled"), then remind the user:
  "Restart any existing Claude Code conversations to pick up the change."
- If exit code is non-zero (and the mode is not `--status`), print the
  stderr output and stop. Do not attempt to retry or "fix" settings.json.
- If the mode is `--status`, exit code 1 just means "not installed" — report
  that plainly without treating it as an error.

Do not modify `~/.claude/settings.json` directly with Edit/Write. The script
performs an atomic write with a `.bak` backup; bypassing it loses that
safety. If the script fails, surface the error to the user and stop.
