---
description: Remove the Windows Task Scheduler cron for the current project (or --all watcher crons)
argument-hint: "[--all]"
---

Remove the Windows Task Scheduler cron entry for the **current project** (the
cwd of this session). Pass `--all` to remove every `ClaudeWatcher_*` task on
the system.

Run exactly:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/scripts/cron-remove.py" $ARGUMENTS
```

After running:

- **Exit code 0**: print the output verbatim — either `removed: <name>` or
  `not registered, nothing to remove: <name>`. Both are success cases (the
  goal — task absent — is achieved).
- **Exit code 2** (bad project dir): surface the error and stop.
- **Other non-zero**: print stderr verbatim and stop. Common causes are
  `schtasks` permission denials or running outside Windows.

This command does not delete the `specs/reports/` directory or any previously
written reports — those remain in the project for the user to inspect or
clean up manually.
