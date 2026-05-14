---
description: Show Windows Task Scheduler status for the current project's watcher cron (or --all)
argument-hint: "[--all]"
---

Report the status of the Windows Task Scheduler cron entry registered for the
**current project** (the cwd of this session). Pass `--all` to list every
`ClaudeWatcher_*` task on the system instead.

Run exactly:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/scripts/cron-status.py" $ARGUMENTS
```

After running:

- **Exit code 0**: print the output verbatim. For a single-project query the
  output includes the schedule (HOURLY/DAILY/MINUTE + repeat), next-run time,
  last-run time + result, status, and the configured action command.
- **Exit code 1** (single-project): means the task is **not registered** for
  this cwd — report that plainly, suggest `/watcher:cron-setup` to register.
  Do not treat as an error.
- **Other non-zero**: print stderr verbatim and stop.

Do not invent or estimate "next run time" if the task is not registered.
