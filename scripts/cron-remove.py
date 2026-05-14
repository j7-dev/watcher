#!/usr/bin/env python3
"""Remove Windows Task Scheduler entries created by /watcher:cron-setup.

Usage:
    cron-remove.py              # remove task for current cwd
    cron-remove.py --project P  # remove task for project P
    cron-remove.py --all        # remove every ClaudeWatcher_* task
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


def task_exists(name: str) -> bool:
    proc = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", name],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def delete_task(name: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", name, "/F"],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def list_all_names() -> list[str]:
    proc = subprocess.run(
        ["schtasks.exe", "/Query", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    names: set[str] = set()
    reader = csv.reader(io.StringIO(proc.stdout or ""))
    for row in reader:
        if not row:
            continue
        tn = row[0].strip()
        if TASK_PREFIX in tn:
            names.add(tn.lstrip("\\"))
    return sorted(names)


def remove_single(name: str, project_hint: Path) -> int:
    if not task_exists(name):
        print(f"not registered, nothing to remove: {name}")
        print(f"project: {project_hint}")
        return 0
    rc, out, err = delete_task(name)
    if rc != 0:
        print("error: schtasks /Delete failed", file=sys.stderr)
        if out.strip():
            print(out.rstrip(), file=sys.stderr)
        if err.strip():
            print(err.rstrip(), file=sys.stderr)
        return rc or 1
    print(f"removed: {name}")
    print(f"project: {project_hint}")
    return 0


def remove_all() -> int:
    names = list_all_names()
    if not names:
        print("no ClaudeWatcher_* tasks to remove")
        return 0
    failures = 0
    for n in names:
        rc, _, err = delete_task(n)
        if rc != 0:
            failures += 1
            print(f"failed: {n}: {err.strip()}", file=sys.stderr)
        else:
            print(f"removed: {n}")
    if failures:
        print(f"{failures} task(s) failed to remove", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Remove Claude watcher cron task(s)"
    )
    p.add_argument("--all", action="store_true", help="remove every ClaudeWatcher_* task")
    p.add_argument(
        "--project",
        default=os.getcwd(),
        help="project directory (default: cwd)",
    )
    args = p.parse_args(argv)

    if args.all:
        return remove_all()

    project_abs = Path(args.project).expanduser().resolve()
    if not project_abs.is_dir():
        print(f"error: project path is not a directory: {project_abs}", file=sys.stderr)
        return 2
    return remove_single(task_name(project_abs), project_abs)


if __name__ == "__main__":
    sys.exit(main())
