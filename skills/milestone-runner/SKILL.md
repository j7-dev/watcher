---
name: milestone-runner
description: Auto-develop every open issue in the next-up GitHub milestone, looping issue-by-issue until the milestone is fully shipped. Detects the current project version, finds the smallest open milestone whose semver is strictly greater, then for each issue inside it spawns a branch, implements the change, runs the project's tests, and on success fast-forwards into the trunk branch (main/master), pushes, and closes the issue. Retries up to 3 times per issue before skipping. Use whenever the user says "run the next milestone", "develop all issues in milestone X", "loop through milestone issues", "implement everything in v1.2", "ship the milestone", "跑下一個 milestone", "把 milestone 的 issue 一個一個做掉", "依序開發 milestone 內 issue"; or any time they want hands-off batch development of grouped issues. Trigger even when the user does not explicitly name a milestone version — the skill resolves the next milestone from the current version on its own.
---

# Milestone Runner

Develop every open issue in the next-up milestone, one after another, fully unattended. The user has opted in to **fully automatic** behaviour: do not pause for confirmation between issues, do not stop at the first failure, and do not open pull requests — push commits directly to the trunk branch after the project's tests pass.

This is destructive-by-design (writes commits, force-fast-forwards, pushes to origin). The user has explicitly chosen this mode. The only points where you may pause are listed under *When to refuse / pause* — everything else runs through.

## When to refuse / pause

Stop and report instead of guessing if any of these hold:

- Working tree is **dirty** (`git status --porcelain` non-empty). Refuse — don't risk mixing in unrelated work.
- `gh` not authenticated, or `git remote get-url origin` empty.
- No milestone with semver strictly greater than the current version is open, or the next one has zero open issues.
- The trunk branch can't be resolved (see *Trunk detection*).
- A pre-push hook on the repo auto-publishes (e.g., watcher's `release.sh` pattern that bumps version every push to master) **and** the user did not explicitly acknowledge it for this run. Warn once with the hook path; if the user re-confirms, proceed.

## The pipeline

Run these steps in order. Print a one-line status after each so the user can follow along.

1. **Verify environment** — `gh auth status`, `git rev-parse --show-toplevel`, working tree clean.
2. **Detect trunk** — see *Trunk detection*.
3. **Detect current version** — same priority order as the `issue-milestone-planner` skill (tag → plugin.json → package.json → pyproject.toml → Cargo.toml). If none match, ask the user once.
4. **Find next milestone** — run `scripts/next_milestone.py` (see *Milestone resolution*).
5. **Detect test command** — see *Test detection*.
6. **Loop over issues** — for each open issue in the milestone, run the *Per-issue loop*.
7. **Summarise** — print a final table: shipped vs. skipped, with reasons for the skips.

## Trunk detection

The user said "push to main / master", which usually means the repo's default branch. Resolve it like this:

```bash
TRUNK=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|origin/||')
[ -z "$TRUNK" ] && TRUNK=$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)
```

If both fail, fall back to checking whether `main` or `master` exists locally. If neither exists, refuse — the user needs to set `origin/HEAD` first.

## Milestone resolution

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/milestone-runner/scripts/next_milestone.py" \
  --current-version "$CURRENT_VERSION" \
  --repo "$REPO"
```

Output JSON:

```json
{
  "milestone": {"number": 4, "title": "v1.2"},
  "issues": [
    {"number": 12, "title": "Refactor config loader", "url": "..."},
    {"number": 7,  "title": "Add CLI flag",          "url": "..."}
  ]
}
```

The script picks the open milestone whose semver is **strictly greater** than the current version and **smallest** among those — i.e., the *immediate* next minor. Issues are ordered by GitHub issue number ascending (oldest first) for determinism. The user is welcome to override the ordering by reassigning issues; otherwise lower numbers ship first.

If the script returns `{"milestone": null}`, stop and tell the user there is nothing to ship.

## Test detection

Run tests after each issue. Detection priority:

1. `package.json` with `scripts.test` → `npm test` (use `pnpm test` / `yarn test` if a `pnpm-lock.yaml` / `yarn.lock` is present)
2. `pyproject.toml` containing `[tool.pytest.ini_options]` or a `tests/` directory → `pytest` (use `uv run pytest` if `uv.lock` is present)
3. `Cargo.toml` → `cargo test`
4. `go.mod` → `go test ./...`
5. `Makefile` with a `test:` target → `make test`
6. `.claude/milestone-runner.local.md` exists with `test_command:` in its frontmatter → use that verbatim (highest priority — user override)

If nothing matches, skip the test step entirely. Don't invent commands.

When the test step is skipped, mention it in the per-issue log so the user knows the work landed without verification — they can choose to populate `.claude/milestone-runner.local.md` next time.

## Per-issue loop

For each issue in the milestone, follow this sub-routine. The user picked retry count `N = 3` by default; if they say "retry N times" with a different number, honour it.

```
1. git checkout $TRUNK
2. git pull --ff-only origin $TRUNK
3. branch="issue/<N>-<slug>"; git checkout -b "$branch"
4. attempt = 1
5. read issue spec: gh issue view <N> --json title,body,labels,comments
6. implement the change (you, Claude, do this — write the code)
7. run test command if detected; if it exits non-zero:
     - read failing output
     - if attempt < N: attempt += 1, go to step 6 with the error in context
     - else: mark this issue SKIPPED with reason="tests failed after N attempts"
       git checkout $TRUNK; git branch -D "$branch"; continue to next issue
8. git add -A; git commit -m "<type>: <subject> (#<N>)"
9. git checkout $TRUNK; git merge --ff-only "$branch"
10. git push origin $TRUNK
11. gh issue close <N> --comment "Shipped via milestone-runner."
12. git branch -d "$branch"
```

A few mechanics worth getting right:

- **Slug**: lower-case ASCII, alnum + hyphen, max 40 chars, derived from the issue title. `Add CLI --json flag!!` → `add-cli-json-flag`. Falls back to `issue-<N>` if the title produces an empty slug.
- **Commit message**: use Conventional Commits if the repo already uses them (check `git log --oneline -20` for `feat:`, `fix:`, `chore:` etc.). Otherwise plain `Subject (#N)`. Always include the issue number so GitHub auto-links.
- **`--ff-only` merge**: keeps history linear and aborts if someone else pushed during the loop — preferable to a silent merge commit. If the fast-forward fails, the loop is racing with another developer; mark this issue SKIPPED with reason="trunk moved during work" and continue. Do not force-push.
- **Branch cleanup on skip**: always `git checkout $TRUNK && git branch -D "$branch"`. Lingering branches accumulate fast.
- **Push failure**: if `git push` fails (network / protected branch / hook reject), mark SKIPPED with the stderr, reset to `origin/$TRUNK`, continue. Do not retry the push automatically.

### Implementation step (step 6) — what "implement the change" means

This is the only step that requires real reasoning. Read the issue carefully (title + body + comments + linked PRs). Then:

- Locate the relevant code with the tools you already have (Grep, Read, Glob, project skills).
- Make the minimum change that closes the issue. Don't refactor neighbouring code that the issue didn't ask about; the loop is moving fast and surprise refactors blow up review cost downstream.
- If the issue is genuinely ambiguous (multiple plausible designs, missing requirements), do **not** ask the user — write down your interpretation as a one-line comment on the issue via `gh issue comment <N> --body "..."` before implementing, so the chosen interpretation is on the record. The loop is opted into automation; the audit trail is the user's safety net.

If you read the issue and discover it is **not actually implementable yet** (depends on unmerged work, requires external decisions, is a discussion / spike not a code change), mark SKIPPED with reason="not implementable: <short why>". Don't fake a commit.

## Summary output

After the loop, print exactly one block like this:

```
Milestone v1.2 (4 issues):
  ✓ #12  Refactor config loader            (2 attempts, tests passed)
  ✓ #7   Add CLI --json flag               (1 attempt,  tests passed)
  ✗ #9   Document the new flag             (skipped: tests failed after 3 attempts)
  ✓ #3   Bump dependency X                 (1 attempt,  tests skipped — no command)

Shipped 3, skipped 1.
```

Then ask the user nothing further — the run is over. They can re-invoke the skill to retry skipped issues after fixing them.

## Audit trail

Each per-issue iteration should log to `.claude/milestone-runner/<run-timestamp>/<issue-N>.log` (create the directory). Capture: the attempts, the diff that landed (or didn't), the test stdout/stderr tails. This is the only place a SKIPPED reason is fully expanded — the summary table only has the one-line version.

## Things that look tempting but are wrong

- **Don't open a PR even if the issue body says "PR welcome".** The user chose direct push. Their workflow.
- **Don't `git rebase` or `git reset --hard` on trunk.** Only fast-forward merge is permitted. If a fast-forward isn't possible the right move is *skip and continue*, never *force the trunk*.
- **Don't keep retrying the same failing approach.** Between retries, the test output is new context — actually use it. If attempt 2 fails for the same reason as attempt 1, you didn't read the error.
- **Don't widen the milestone selection.** This skill ships *one* milestone per invocation. If the user wants the milestone after that, they re-invoke. Looping across milestones risks shipping work whose dependencies haven't landed yet (the `issue-milestone-planner` skill assumed milestone order matters).
- **Don't auto-resolve cycles or merge skipped issues into "the next run".** Skipped means skipped; the user gets to decide.

## Relationship to `issue-milestone-planner`

These two skills are a pair. `issue-milestone-planner` decides *which* milestone an issue goes into, by dependency. `milestone-runner` walks one of those milestones and ships everything inside. The natural workflow is `planner → runner → runner → ...`, one runner invocation per minor version.
