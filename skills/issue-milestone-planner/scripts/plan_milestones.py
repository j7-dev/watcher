#!/usr/bin/env python3
"""Plan milestones from issues + dependency edges.

Read input JSON (issues + edges) and current version, emit milestone buckets so
that every issue's milestone index is strictly greater than the index of any
issue it depends on. Each milestone holds at most `--cap` issues; overflow rolls
forward, dragging dependents along.

Input  (stdin or --input):
  {
    "issues": [{"number": int, "title": str}, ...],
    "edges":  [[child, parent], ...]      # child depends on parent
  }

Output (stdout or --output):
  {
    "milestones": [{"title": "v1.2", "issues": [12, 7]}, ...],
    "skipped":    [{"number": 18, "reason": "cycle"}]
  }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

SEMVER_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_current_version(s: str) -> tuple[int, int]:
    m = SEMVER_RE.search(s.strip())
    if not m:
        raise SystemExit(f"cannot parse version: {s!r}")
    return int(m.group(1)), int(m.group(2))


def next_minor_titles(major: int, minor: int, count: int) -> list[str]:
    return [f"v{major}.{minor + i + 1}" for i in range(count)]


def plan(
    issues: list[dict],
    edges: list[tuple[int, int]],
    current_version: str,
    cap: int,
) -> dict:
    issue_numbers = {i["number"] for i in issues}
    # Keep only edges where both endpoints are in scope (open issues we're planning).
    edges = [(c, p) for c, p in edges if c in issue_numbers and p in issue_numbers]

    # Adjacency: for each node, which parents it depends on, and which children depend on it.
    parents: dict[int, set[int]] = defaultdict(set)
    children: dict[int, set[int]] = defaultdict(set)
    for c, p in edges:
        if c == p:
            continue
        parents[c].add(p)
        children[p].add(c)

    # Kahn's topological sort. Ties broken by issue number for determinism.
    indegree = {n: len(parents[n]) for n in issue_numbers}
    ready = deque(sorted(n for n in issue_numbers if indegree[n] == 0))
    topo: list[int] = []
    while ready:
        n = ready.popleft()
        topo.append(n)
        for ch in sorted(children[n]):
            indegree[ch] -= 1
            if indegree[ch] == 0:
                ready.append(ch)

    skipped = [
        {"number": n, "reason": "cycle"}
        for n in sorted(issue_numbers - set(topo))
    ]

    # Bucket: each issue goes into max(parent_bucket)+1 or later, respecting cap.
    bucket_of: dict[int, int] = {}
    buckets: list[list[int]] = []
    for n in topo:
        earliest = 0
        for p in parents[n]:
            if p in bucket_of:
                earliest = max(earliest, bucket_of[p] + 1)
        idx = earliest
        while True:
            while idx >= len(buckets):
                buckets.append([])
            if len(buckets[idx]) < cap:
                buckets[idx].append(n)
                bucket_of[n] = idx
                break
            idx += 1

    major, minor = parse_current_version(current_version)
    titles = next_minor_titles(major, minor, len(buckets))

    return {
        "milestones": [
            {"title": titles[i], "issues": buckets[i]}
            for i in range(len(buckets))
            if buckets[i]
        ],
        "skipped": skipped,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--current-version", required=True)
    ap.add_argument("--cap", type=int, default=20)
    ap.add_argument("--input", type=Path, help="JSON input file (default: stdin)")
    ap.add_argument("--output", type=Path, help="JSON output file (default: stdout)")
    args = ap.parse_args()

    raw = args.input.read_text() if args.input else sys.stdin.read()
    data = json.loads(raw)

    issues = data.get("issues", [])
    edges = [tuple(e) for e in data.get("edges", [])]

    result = plan(issues, edges, args.current_version, args.cap)
    payload = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        args.output.write_text(payload + "\n")
    else:
        print(payload)


if __name__ == "__main__":
    main()
