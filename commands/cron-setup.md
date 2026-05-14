---
description: Register a Windows Task Scheduler cron for the current project to periodically wake claude -p
argument-hint: "[interval]  e.g. 5h (default), 2d, 30m"
---

Register a Windows Task Scheduler entry for the **current project** (the cwd
in which this Claude Code session was started). Every interval the task wakes
`claude -p` to observe the project state and write a markdown report into
`<project>/specs/reports/`. The AI itself decides whether the project is
still in development or already shipped, then tailors the report accordingly.

Run exactly:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/scripts/cron-setup.py" $ARGUMENTS
```

Interval formats accepted by `$ARGUMENTS`:

- (empty) — `5h` default
- `N`     — N hours (bare digit treated as hours)
- `Nh`    — N hours (1–23)
- `Nd`    — N days (1–365)
- `Nm`    — N minutes (1–1439)

After running:

- **Exit code 0**: print the registration summary verbatim (task name,
  project, schedule, claude path, github status). Remind the user the task
  runs only while a user is logged in (`/IT`) and **does not wake the PC
  from sleep**. If the `github:` line reports gh missing / unauthenticated /
  no remote, mention that issues will not be published but reports will
  still be written.
- **Exit code 2** (bad project dir): surface the error and stop.
- **Exit code 3** (claude CLI not on PATH): surface the error verbatim — the
  user must install Claude Code or fix PATH before retrying.
- **Other non-zero**: print stderr verbatim (likely a `schtasks` permission
  or duplicate-name conflict) and stop. Do **not** attempt to "fix" the task
  manually with another `schtasks` call.

After the cron runs, findings classified by the AI as **Bug / Feature / Task**
are published to the project's GitHub repository as labeled issues, with
duplicates (open issues sharing the same stable signature) silently skipped.
Each report file ends with an `## Issue publishing summary` section listing
attempted / created / skipped / errors.

Do not edit Windows Task Scheduler directly with `schtasks.exe` from outside
this script — the script is the single source of truth for task naming and
action format.
