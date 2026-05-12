#!/usr/bin/env python3
"""Find the next-up milestone after the current project version.

The "next" milestone is the open milestone whose semver is *strictly greater*
than the current version, and *smallest* among those (i.e. the immediate next
minor — not three releases ahead). Returns its open issues in ascending number
order so the caller's loop is deterministic.

Output:
  {"milestone": {"number": int, "title": str} | null,
   "issues":    [{"number": int, "title": str, "url": str}, ...]}
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

SEMVER_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_semver(s: str) -> tuple[int, int, int] | None:
    m = SEMVER_RE.search(s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit(r.returncode)
    return r.stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--current-version", required=True)
    ap.add_argument("--repo", required=True, help='Format: "owner/name"')
    args = ap.parse_args()

    current = parse_semver(args.current_version)
    if current is None:
        raise SystemExit(f"cannot parse --current-version: {args.current_version!r}")

    raw = gh("api", "--paginate", f"repos/{args.repo}/milestones?state=open&per_page=100")
    # `--paginate` concatenates JSON arrays with no separator; split manually.
    milestones: list[dict] = []
    decoder = json.JSONDecoder()
    idx = 0
    raw_stripped = raw.strip()
    while idx < len(raw_stripped):
        chunk, end = decoder.raw_decode(raw_stripped, idx)
        if isinstance(chunk, list):
            milestones.extend(chunk)
        else:
            milestones.append(chunk)
        idx = end
        while idx < len(raw_stripped) and raw_stripped[idx] in " \r\n\t":
            idx += 1

    candidates = []
    for m in milestones:
        sv = parse_semver(m.get("title", ""))
        if sv is None or sv <= current:
            continue
        if m.get("open_issues", 0) == 0:
            continue
        candidates.append((sv, m))

    if not candidates:
        print(json.dumps({"milestone": None, "issues": []}))
        return

    candidates.sort(key=lambda x: x[0])
    _, target = candidates[0]

    raw_issues = gh(
        "issue", "list",
        "--repo", args.repo,
        "--state", "open",
        "--milestone", target["title"],
        "--limit", "1000",
        "--json", "number,title,url",
    )
    issues = sorted(json.loads(raw_issues), key=lambda i: i["number"])

    print(json.dumps({
        "milestone": {"number": target["number"], "title": target["title"]},
        "issues": issues,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
