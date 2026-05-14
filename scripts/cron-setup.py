#!/usr/bin/env python3
"""Register a Windows Task Scheduler entry for the current project.

Creates / updates a per-project cron task that periodically wakes
`claude -p` to observe project state and write a markdown report into
`<project>/specs/reports/`.

Usage:
    cron-setup.py [interval]
    cron-setup.py --project PATH [interval]

Interval formats:
    (empty)  -> 5h   (default)
    N        -> N hours (bare digit = hours)
    Nh / NH  -> N hours  (1-23)
    Nd / ND  -> N days   (1-365)
    Nm / NM  -> N minutes (1-1439)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


INTERVAL_RE = re.compile(r"^(?P<num>\d+)(?P<unit>[hHdDmM])?$")

UNIT_BOUNDS = {
    "m": (1, 1439, "MINUTE"),
    "h": (1, 23, "HOURLY"),
    "d": (1, 365, "DAILY"),
}

DEFAULT_INTERVAL = "5h"


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def task_name(project_abs: Path) -> str:
    norm = str(project_abs.resolve()).lower().replace("\\", "/")
    sha8 = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    return f"ClaudeWatcher_{sha8}"


def parse_interval(raw: str) -> tuple[str, int]:
    """Return (schtasks_unit, value). Raises SystemExit on invalid input."""
    raw = (raw or "").strip() or DEFAULT_INTERVAL
    m = INTERVAL_RE.match(raw)
    if not m:
        raise SystemExit(
            f"error: invalid interval '{raw}'. expected forms: 5 / 5h / 2d / 30m"
        )
    num = int(m.group("num"))
    unit = (m.group("unit") or "h").lower()
    if unit not in UNIT_BOUNDS:
        raise SystemExit(f"error: unknown unit '{unit}' in '{raw}'")
    lo, hi, sch_unit = UNIT_BOUNDS[unit]
    if not (lo <= num <= hi):
        hint = ""
        if unit == "h" and num > 23:
            hint = f" hint: use '{round(num / 24)}d' for daily intervals"
        elif unit == "m" and num > 1439:
            hint = " hint: use 'h' or 'd' for longer intervals"
        raise SystemExit(
            f"error: {sch_unit} interval {num} out of range [{lo}, {hi}].{hint}"
        )
    return sch_unit, num


def check_claude_cli() -> str | None:
    """Return absolute path to claude CLI, or None if missing."""
    path = shutil.which("claude")
    if path:
        return path
    return None


def check_gh_cli(project_abs: Path) -> tuple[bool, bool, str]:
    """Return (gh_on_path, repo_detected, hint).

    Non-blocking: cron-setup proceeds even if gh / remote is missing. The
    runner falls back to report-only mode at runtime.
    """
    gh = shutil.which("gh")
    if not gh:
        return False, False, "gh CLI not on PATH (issue publishing will be skipped)"
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return True, False, "gh CLI present but not authenticated (`gh auth login`); issue publishing will be skipped"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True, False, "gh auth check timed out; issue publishing may be skipped"
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True, text=True, timeout=10,
            cwd=str(project_abs),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return True, True, ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return True, False, "no GitHub remote detected for this project (issue publishing will be skipped)"


def build_action(plugin_root_path: Path, project_abs: Path) -> str:
    runner = plugin_root_path / "scripts" / "cron-runner.ps1"
    return (
        f'pwsh -NoProfile -WindowStyle Hidden -File '
        f'"{runner}" -ProjectPath "{project_abs}"'
    )


def run_schtasks_create(
    name: str, sch_unit: str, value: int, action: str
) -> tuple[int, str, str]:
    cmd = [
        "schtasks.exe",
        "/Create",
        "/TN", name,
        "/SC", sch_unit,
        "/MO", str(value),
        "/TR", action,
        "/RL", "LIMITED",
        "/IT",
        "/F",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Register a Claude watcher cron task for the current project"
    )
    p.add_argument(
        "interval",
        nargs="?",
        default="",
        help="interval (e.g. 5, 5h, 2d, 30m); default 5h",
    )
    p.add_argument(
        "--project",
        default=os.getcwd(),
        help="project directory (default: cwd)",
    )
    args = p.parse_args(argv)

    project_abs = Path(args.project).expanduser().resolve()
    if not project_abs.is_dir():
        print(f"error: project path is not a directory: {project_abs}", file=sys.stderr)
        return 2

    sch_unit, value = parse_interval(args.interval)

    claude_path = check_claude_cli()
    if not claude_path:
        print(
            "error: 'claude' CLI not found on PATH. install Claude Code and ensure "
            "claude.exe is reachable, then retry.",
            file=sys.stderr,
        )
        return 3

    gh_ok, repo_ok, gh_hint = check_gh_cli(project_abs)

    name = task_name(project_abs)
    action = build_action(plugin_root(), project_abs)

    rc, out, err = run_schtasks_create(name, sch_unit, value, action)
    if rc != 0:
        print("error: schtasks /Create failed", file=sys.stderr)
        if out.strip():
            print(out.rstrip(), file=sys.stderr)
        if err.strip():
            print(err.rstrip(), file=sys.stderr)
        return rc or 1

    human_interval = {
        "MINUTE": f"every {value} minute(s)",
        "HOURLY": f"every {value} hour(s)",
        "DAILY": f"every {value} day(s)",
    }[sch_unit]

    print(f"registered: {name}")
    print(f"project:    {project_abs}")
    print(f"schedule:   {human_interval}")
    print(f"claude:     {claude_path}")
    if repo_ok:
        print("github:     remote detected; findings will be published as labeled issues (Bug/Feature/Task)")
    elif gh_hint:
        print(f"github:     {gh_hint}")
    print("note: task runs only while a user is logged in (/IT) and does not wake the PC")
    print(f"        reports will appear in {project_abs}\\specs\\reports\\")
    return 0


if __name__ == "__main__":
    sys.exit(main())
