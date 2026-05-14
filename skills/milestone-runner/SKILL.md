---
name: milestone-runner
description: Auto-develop every open issue in the next-up GitHub milestone, looping issue-by-issue until the milestone is fully shipped. Detects the current project version, finds the smallest open milestone whose semver is strictly greater, then for each issue inside it spawns a branch, implements the change, runs the project's tests, and on success fast-forwards into the trunk branch (main/master), pushes, and closes the issue. Retries up to 3 times per issue before skipping. Use whenever the user says "run the next milestone", "develop all issues in milestone X", "loop through milestone issues", "implement everything in v1.2", "ship the milestone", "跑下一個 milestone", "把 milestone 的 issue 一個一個做掉", "依序開發 milestone 內 issue"; or any time they want hands-off batch development of grouped issues. Trigger even when the user does not explicitly name a milestone version — the skill resolves the next milestone from the current version on its own.
---

# Milestone Runner

Develop every open issue in the next-up milestone, one after another, fully unattended. The user has opted in to **fully automatic** behaviour: do not pause for confirmation between issues, do not stop at the first failure, and do not open pull requests — push commits directly to the trunk branch after the project's tests pass.

This is destructive-by-design (writes commits, force-fast-forwards, pushes to origin). The user has explicitly chosen this mode. The only points where you may pause are listed under *When to refuse / pause* — everything else runs through.

## Context isolation (read this before anything else)

The user explicitly asked for `/clear`-style context isolation between issues so that earlier issues' details do not pollute later ones. A literal `/clear` cannot be invoked from inside a running skill — it terminates the very conversation that is driving the loop, killing the run. The correct primitive is **per-issue subagent dispatch**:

- The **main thread** orchestrates: detects version, resolves the milestone, iterates the issue list, and prints the final summary. It never reads issue bodies, runs tests, or edits code.
- For each issue, the main thread spawns **one subagent** via the `Agent` tool with `subagent_type: "general-purpose"`. The subagent receives a fully self-contained briefing (issue number, title, body, repo path, trunk branch, test command, retry count, commit-style guidance) and does the entire per-issue routine in its own isolated context.
- The subagent returns a short structured result (status + one-line reason + commit SHA if any). That return is the **only** thing that enters the main thread's context — typically <100 tokens per issue.

This achieves what `/clear` would, while preserving the loop state (milestone progress, already-shipped list) that `/clear` would erase. Subagents see only their own issue; the main thread sees only progress, not implementation details.

If subagents are unavailable in the current environment (rare — older Claude Code without `Agent`), fall back to sequential in-thread execution and warn the user that context will accumulate. Do not silently degrade.

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
6. **Ensure `prosessing` label exists** — create it once before the loop. Idempotent:
   ```bash
   gh label create prosessing \
     --color FBCA04 \
     --description "Currently being worked on by milestone-runner" \
     2>/dev/null || true
   ```
   The label marks an issue as actively in-flight so a human watching the issue tracker can see "someone (the runner) is on it right now". It is added when the subagent starts and removed before the issue closes, regardless of success or skip.
7. **Loop over issues** — for each open issue in the milestone, **dispatch one subagent** that runs the *Per-issue routine*. Wait for it to return, append its result to the in-memory summary list, move on.
8. **Summarise** — print a final table: shipped vs. skipped, with reasons for the skips. Then, if `$WEZTERM_PANE` is set, write a completion marker at `${XDG_STATE_HOME:-$HOME/.local/state}/watcher/milestone-done/$WEZTERM_PANE.json` containing `{"pane": "$WEZTERM_PANE", "milestone": "<resolved>", "completed_at": "<ISO timestamp>", "shipped": <n>, "skipped": <n>}`. The watcher daemon checks this file's mtime against the toggle's `enabled_at` to auto-disable the `/watcher:milestone-on` per-pane re-injector; without it the daemon will keep re-firing `/milestone-runner` on every Stop event.

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

## Per-issue routine (runs inside the subagent)

The main thread dispatches one subagent per issue. The subagent must operate from a **single self-contained prompt** because it does not share the main thread's memory.

### How the main thread dispatches

For each issue, call:

```
Agent(
  description: "Ship issue #<N>",
  subagent_type: "general-purpose",
  prompt: <the full briefing — see template below>
)
```

Wait for the subagent to finish before dispatching the next one (sequential, not parallel — they all push to trunk and would race). Capture the returned status line and append it to the summary list. Do **not** re-read or re-summarise the subagent's transcript; the one-line return is the contract.

### Subagent briefing template

Fill all `{placeholders}` from the main thread's state and pass as the `prompt`:

```
You are a milestone-runner subagent. You exist for ONE GitHub issue and nothing else. You have a fresh context — no prior conversation, no other issues. When you're done, return exactly one status line and stop.

REPO:           {repo_owner_and_name}
LOCAL PATH:     {git_toplevel_absolute_path}
TRUNK BRANCH:   {trunk}
ISSUE NUMBER:   #{N}
ISSUE TITLE:    {title}
ISSUE URL:      {url}
TEST COMMAND:   {test_cmd or "(none — skip the test step)"}
RETRY BUDGET:   {N_retries, default 3}

YOUR JOB — run these steps in order, in the local path above:

1. `cd` to LOCAL PATH. Verify working tree clean (`git status --porcelain` empty).
2. `git checkout {trunk} && git pull --ff-only origin {trunk}`.
3. Create branch `issue/{N}-<slug>`. Slug rule: lower-case ASCII, alnum + hyphen, max 40 chars from ISSUE TITLE; if empty, use `issue-{N}`.
4. **Mark the issue in-flight**: `gh issue edit {N} --add-label prosessing`. This must happen *before* implementation so a human watching can see you've started. From this point on, **every exit path** — success or skip — must call `gh issue edit {N} --remove-label prosessing` exactly once before returning. If you forget, the issue is left with a stale label and the next run might think it's still in progress.
5. Fetch the full spec yourself: `gh issue view {N} --json title,body,labels,comments`. Read every comment. If linked PRs exist, glance at them too.
6. Implement the minimum change that closes the issue. Use Grep/Read/Glob/Edit/Write. Do NOT refactor unrelated code. If the issue is ambiguous, post a one-line `gh issue comment {N} --body "Interpretation: ..."` documenting your reading, then implement that reading. Do not ask the user — there is no user reachable from inside a subagent.
7. If a TEST COMMAND was given, run it. On non-zero exit: read the output, refine the change, retry. Repeat up to RETRY BUDGET attempts. If still failing: remove the `prosessing` label, then go to step 10 with status="skipped: tests failed after {N_retries} attempts".
8. `git add -A && git commit -m "<msg>"`. **Commit message format is fixed**: `#{N} <subject>`. The `#{N}` prefix is mandatory so the commit links back to the issue on GitHub. `<subject>` is a short imperative sentence in the issue's primary language (Chinese for Chinese issues, English for English issues — match the issue title's language). Do **not** prepend `feat:`/`fix:`/`chore:` etc. even if the rest of the repo uses Conventional Commits — the `#{N}` prefix replaces that role here. Example: `#12 優化 Loading 樣式`. Example: `#37 Fix race in config reload`.
9. `git checkout {trunk} && git merge --ff-only issue/{N}-<slug> && git push origin {trunk}`. If ff-only fails, the trunk moved during your work — remove the `prosessing` label, status="skipped: trunk moved during work", go to step 10. If push fails, capture stderr — remove the `prosessing` label, status="skipped: push rejected: <one-line stderr>", go to step 10.
10. `git checkout {trunk}` and delete the branch (`-d` after merge, `-D` after skip).
11. **On success only**: `gh issue edit {N} --remove-label prosessing && gh issue close {N} --comment "Shipped via milestone-runner."`. Closing the issue does **not** auto-remove the label — both calls are needed.

Special early-exit: if reading the issue reveals it is not implementable (depends on unmerged work, is a discussion/spike, requires external decision), remove the `prosessing` label, then skip to step 10 with status="skipped: not implementable: <one-line why>". Don't fake a commit.

Label-removal safety: if for any reason a step fails *after* the label was added but *before* it was removed (subagent crash, unexpected exception), the next run will re-encounter an issue tagged `prosessing`. That's a recoverable nuisance, not a hazard — the user can manually strip the label or the next milestone-runner invocation will overwrite it when it touches the same issue.

LOGS: write everything to `{audit_dir}/issue-{N}.log` (create the directory): attempts, diffs that landed, test stdout/stderr tails, any gh CLI output. This is the user's only safety net.

RETURN FORMAT (this is the entire output you give back — nothing else):

  STATUS: shipped | skipped
  ISSUE: #{N}
  TITLE: <issue title>
  ATTEMPTS: <int>
  TESTS: passed | failed | skipped
  COMMIT: <sha or "-">
  REASON: <one-line — only meaningful when STATUS=skipped>

Be terse. Do not write a tutorial. Do not echo this briefing back. Just do the work and return the status line.
```

### Mechanics worth getting right

These apply inside the subagent. They are in the briefing above but spelled out here for the skill maintainer's reference:

- **Slug**: lower-case ASCII, alnum + hyphen, max 40 chars from the issue title. `Add CLI --json flag!!` → `add-cli-json-flag`. Falls back to `issue-<N>` if the title produces an empty slug.
- **Commit message**: fixed format `#<N> <subject>` (e.g. `#12 優化 Loading 樣式`). No `feat:` / `fix:` prefix even when the surrounding repo uses Conventional Commits — the user explicitly chose the `#<N>` prefix as the canonical link-back, and mixing both styles fragments the log.
- **`prosessing` label**: added at step 4, removed on every exit path (step 7 fail / step 9 race / step 9 push-fail / step 11 success / not-implementable early exit). Forgetting to remove it is the most common subagent bug — every `status=skipped` branch in the briefing names the removal explicitly.
- **`--ff-only` merge**: keeps history linear, aborts on race. Race → skip and continue. Never force-push.
- **Branch cleanup on skip**: `git checkout $TRUNK && git branch -D "$branch"` — lingering branches accumulate fast across runs.
- **Push failure**: capture stderr into the return line, reset working state, skip. Do not retry the push automatically.
- **Subagent ambiguity**: a subagent cannot ask the user. Its `gh issue comment` is the escape hatch — it records the interpretation publicly, then proceeds.

### What the main thread does between subagents

Nothing complicated:

1. Read the returned status line.
2. Append it to the summary list (just the line, nothing else).
3. Move on to the next issue.

Do not load logs, re-read commits, or inspect the working tree between subagents — the main thread's value is staying clean. Trust the subagent's return.

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

The main thread creates the run directory **once** at the start of the loop, then passes its path to each subagent in the briefing's `{audit_dir}` placeholder:

```
audit_dir = .claude/milestone-runner/<run-timestamp>
```

Each subagent writes its own `issue-<N>.log` inside that directory: attempts, diffs that landed (or didn't), test stdout/stderr tails. This is the only place a SKIPPED reason is fully expanded — the summary table only has the one-line version, and the main thread does not see anything else.

After the loop, optionally print the audit directory path at the bottom of the summary so the user knows where to look.

## Things that look tempting but are wrong

- **Don't invoke `/clear` mid-loop.** It kills the running conversation and the loop dies with it. The subagent dispatch above is the correct isolation primitive.
- **Don't dispatch subagents in parallel.** They all push to the same trunk; parallel runs race on the ff-only push and most will fail. Sequential is the point.
- **Don't have the main thread read the subagent's transcript.** The whole reason for the subagent is to keep that transcript out of the main thread's context. The one-line return is the contract.
- **Don't open a PR even if the issue body says "PR welcome".** The user chose direct push. Their workflow.
- **Don't `git rebase` or `git reset --hard` on trunk.** Only fast-forward merge is permitted. If a fast-forward isn't possible the right move is *skip and continue*, never *force the trunk*.
- **Don't keep retrying the same failing approach.** Between retries, the test output is new context — actually use it. If attempt 2 fails for the same reason as attempt 1, you didn't read the error.
- **Don't widen the milestone selection.** This skill ships *one* milestone per invocation. If the user wants the milestone after that, they re-invoke. Looping across milestones risks shipping work whose dependencies haven't landed yet (the `issue-milestone-planner` skill assumed milestone order matters).
- **Don't auto-resolve cycles or merge skipped issues into "the next run".** Skipped means skipped; the user gets to decide.

## Relationship to `issue-milestone-planner`

These two skills are a pair. `issue-milestone-planner` decides *which* milestone an issue goes into, by dependency. `milestone-runner` walks one of those milestones and ships everything inside. The natural workflow is `planner → runner → runner → ...`, one runner invocation per minor version.
