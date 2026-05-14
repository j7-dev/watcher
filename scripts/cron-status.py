#!/usr/bin/env python3
"""Show Windows Task Scheduler status for Claude watcher cron tasks.

Usage:
    cron-status.py              # status of task for current cwd
    cron-status.py --project P  # status of task for project P
    cron-status.py --all        # list every ClaudeWatcher_* task on the system
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path


TASK_PREFIX = "ClaudeWatcher_"


def task_name(project_abs: Path) -> str:
    norm = str(project_abs.resolve()).lower().replace("\\", "/")
    sha8 = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    return f"{TASK_PREFIX}{sha8}"


def query_single(name: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", name, "/FO", "LIST", "/V"],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def query_all_csv() -> tuple[int, str, str]:
    proc = subprocess.run(
        ["schtasks.exe", "/Query", "/FO", "CSV", "/V", "/NH"],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def parse_list_output(text: str) -> dict[str, str]:
    """Parse schtasks /FO LIST output into a key->value dict (last record only)."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fields[key.strip()] = val.strip()
    return fields


def format_single(name: str, project_hint: Path) -> int:
    rc, out, err = query_single(name)
    if rc != 0:
        msg = (err or out).strip().splitlines()
        last = msg[-1] if msg else ""
        if "ERROR:" in last and ("does not exist" in last or "cannot find" in last):
            print(f"not registered: {name}")
            print(f"project:        {project_hint}")
            return 1
        print("error: schtasks query failed", file=sys.stderr)
        if out.strip():
            print(out.rstrip(), file=sys.stderr)
        if err.strip():
            print(err.rstrip(), file=sys.stderr)
        return rc or 1

    f = parse_list_output(out)
    schedule_type = f.get("Schedule Type", "?")
    repeat_every = f.get("Repeat: Every", "")
    next_run = f.get("Next Run Time", "?")
    last_run = f.get("Last Run Time", "?")
    last_result = f.get("Last Result", "?")
    status = f.get("Status", "?")
    action = f.get("Task To Run", "?")

    print(f"registered:   {name}")
    print(f"project:      {project_hint}")
    print(f"schedule:     {schedule_type}" + (f" / repeat: {repeat_every}" if repeat_every else ""))
    print(f"status:       {status}")
    print(f"next run:     {next_run}")
    print(f"last run:     {last_run}  (result: {last_result})")
    print(f"action:       {action}")
    return 0


def format_all() -> int:
    rc, out, err = query_all_csv()
    if rc != 0:
        print("error: schtasks query /FO CSV failed", file=sys.stderr)
        if err.strip():
            print(err.rstrip(), file=sys.stderr)
        return rc or 1

    reader = csv.reader(io.StringIO(out))
    headers_seen = False
    matches: list[tuple[str, str, str, str]] = []
    header_idx = {}
    expected_headers = {"TaskName", "Next Run Time", "Status", "Last Run Time"}

    for row in reader:
        if not row:
            continue
        if not headers_seen:
            if expected_headers.issubset(set(row)):
                header_idx = {h: i for i, h in enumerate(row)}
                headers_seen = True
            continue
        if headers_seen and not row:
            headers_seen = False
            continue
        tn = row[header_idx.get("TaskName", 0)] if row else ""
        if TASK_PREFIX not in tn:
            continue
        matches.append((
            tn.strip(),
            row[header_idx.get("Status", 2)] if len(row) > 2 else "?",
            row[header_idx.get("Next Run Time", 1)] if len(row) > 1 else "?",
            row[header_idx.get("Last Run Time", 3)] if len(row) > 3 else "?",
        ))

    if not matches:
        print("no ClaudeWatcher_* tasks registered")
        return 0

    seen: set[str] = set()
    print(f"{'TASK':<32} {'STATUS':<12} {'NEXT RUN':<22} LAST RUN")
    for tn, st, nx, lr in matches:
        if tn in seen:
            continue
        seen.add(tn)
        short = tn.lstrip("\\")
        print(f"{short:<32} {st:<12} {nx:<22} {lr}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Show Claude watcher cron task status"
    )
    p.add_argument("--all", action="store_true", help="list all ClaudeWatcher_* tasks")
    p.add_argument(
        "--project",
        default=os.getcwd(),
        help="project directory (default: cwd)",
    )
    args = p.parse_args(argv)

    if args.all:
        return format_all()

    project_abs = Path(args.project).expanduser().resolve()
    if not project_abs.is_dir():
        print(f"error: project path is not a directory: {project_abs}", file=sys.stderr)
        return 2
    return format_single(task_name(project_abs), project_abs)


if __name__ == "__main__":
    sys.exit(main())
