"""Watcher daemon: monitor tmux panes running Claude Code, ask Codex CLI what to
reply, then send keys back via tmux.

Run: `uv run watcher.py`  (or `uv run watcher`)
     `uv run watcher.py --once`     → single discovery/classification tick, no codex
     `uv run watcher.py --dry-run`  → full loop but skip the final send-keys
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import deque
from pathlib import Path
from typing import Any

WATCHER_DIR = Path(__file__).resolve().parent
CONFIG_PATH = WATCHER_DIR / "config.toml"
SCHEMA_PATH = WATCHER_DIR / "response_schema.json"
LOG_DIR = WATCHER_DIR / "logs"
TRIGGER_DIR = LOG_DIR / "triggers"

PROMPT_TEMPLATE = """\
You are an auto-response decision agent for a Claude Code terminal session.
Below is the current screen capture of a Claude Code pane that has been idle
(waiting for user input) for several seconds.

Decide ONE action and reply with JSON matching the provided schema. The
`value` field is required in every response — use null when it does not apply:
  - action="text"   value=<reply text>            → type free-text reply, press Enter
  - action="key"    value="1" | "2" | ...         → press a single digit for a menu choice
  - action="enter"  value=null                    → press Enter only (accept default)
  - action="skip"   value=<short reason>          → refuse to respond (ambiguous/dangerous)

PREFER MAKING A DECISION OVER SKIPPING. You have NO context beyond the screen
below; do not invent details, but do use what is visible.

Decision rules:
  - If the screen shows numbered options (e.g. "1. ... 2. ..."), PICK ONE.
    When the choice is between an elevated-permission path (sudo, root, system
    package install, modifying global state) and a software-only fallback that
    achieves the same goal, prefer the fallback unless the screen explicitly
    states the elevated path is required or preferred.
  - For a `❯` free-text input box, prefer action="text" with a concise reply
    that answers the visible question. If the latest question is a clear yes/no
    or one-word check, answer it directly.
  - For an `❯ 1.` style menu line, prefer action="key" with the matching digit.

Skip ONLY when one of these holds:
  1. The prompt asks for information you cannot possibly infer from the screen
     (passwords, API keys, secrets, personal data).
  2. Acting wrong would lose work irreversibly (rm -rf, force-push, DROP TABLE,
     git reset --hard on dirty tree, deleting branches with unmerged commits).
  3. No actual question or menu is visible — the pane is just idle.

--- SCREEN CAPTURE (between fences) ---
```
{screen}
```
"""

MENU_CHOICE_RE = re.compile(r"^\s*❯\s+\d+[.)\]]")
HR_DASH_THRESHOLD = 50  # min count of `─` chars on a line to call it a horizontal rule

# Built-in question markers checked by the pre-codex skip predictor. A captured
# screen that classifies as `input` but contains NONE of these (and no numbered
# list line) in the lookback window is assumed to be an idle pane with no
# pending question — codex would almost certainly answer "skip", so we short-
# circuit and save the round-trip. Users can extend via
# config.skip_predictor_extra_markers (case-insensitive substring match).
DEFAULT_QUESTION_MARKERS: tuple[str, ...] = (
    "?", "？",
    "do you", "would you", "shall i", "should i",
    "continue", "confirm", "proceed", "approve",
    "press", "choose", "select", "pick",
    "y/n", "yes/no", "(y/n)",
    "是否", "要不要", "請選", "請輸入", "請問", "確認",
)
NUMBERED_LIST_RE = re.compile(r"^\s*\d+[.)]\s")

# Module-level decision cache: screen-hash → (expiry_ts, decision_dict).
# Only `skip` decisions are stored; other actions mutate the pane so the same
# screen won't recur. Cache is in-memory and dies with the daemon.
_DECISION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_MAX_ENTRIES = 256


def is_hr_line(line: str) -> bool:
    """A Claude horizontal rule may have a session label embedded (top rule)
    or be pure dashes (bottom rule). Both contain many `─` characters."""
    return line.count("─") >= HR_DASH_THRESHOLD


def is_empty_prompt_line(line: str) -> bool:
    """Claude renders the empty input as `❯ ` (NBSP after `❯`) — possibly with
    a dim placeholder hint like `Try "how does <filepath> work?"`."""
    s = line.strip()
    if not s.startswith("❯"):
        return False
    rest = s[1:].strip()
    if not rest:
        return True
    return rest.startswith('Try "') and rest.endswith('"')

WORKING_HINTS = ("esc to interrupt", "(ctrl+o to expand)")

# Claude Code shows queued draft messages below the input box prefixed with
# fullwidth `｜` (U+FF5C). When present the user has already typed their next
# reply — auto-responder should leave the pane alone.
QUEUE_MARKER = "｜"

# ---------- chrome filter for codex prompt ------------------------------------
# Strip pane chrome that adds noise without information (welcome banner, status
# line, footer hints, spinner counters) so codex sees only the conversation.
_WELCOME_MARKERS = ("Welcome back", "Tips for getting started", "/release-notes for more")
_STATUSLINE_RE = re.compile(r"📂")               # status line: model badge/dir/branch (📂 is unique anchor)
_FOOTER_RE     = re.compile(r"^\s*⏵⏵\s+(bypass|auto-accept)")  # bottom mode hint
_BAKED_RE      = re.compile(r"^\s*✻\s+\w+\s+for\s+")            # spinner counter line
_TOKEN_RE      = re.compile(r"^\s*\d+\s+tokens\s*$")            # `114738 tokens` line


def _clean_screen(screen: str) -> str:
    lines = screen.splitlines()
    start = 0
    head = "\n".join(lines[:25])
    if any(m in head for m in _WELCOME_MARKERS):
        for i, l in enumerate(lines[:30]):
            if l.lstrip().startswith("╰"):
                start = i + 1
                break
    kept = []
    for l in lines[start:]:
        if _STATUSLINE_RE.search(l):
            continue
        if _FOOTER_RE.match(l):
            continue
        if _BAKED_RE.match(l):
            continue
        if _TOKEN_RE.match(l):
            continue
        kept.append(l)
    return "\n".join(kept).strip("\n")

log = logging.getLogger("watcher")


# ---------- skip predictor & decision cache -----------------------------------

def _build_markers(cfg: dict[str, Any]) -> list[str]:
    extras = cfg.get("skip_predictor_extra_markers") or []
    extra_lc = [str(m).lower() for m in extras if str(m).strip()]
    return [*DEFAULT_QUESTION_MARKERS, *extra_lc]


def predict_skip(
    clean_screen: str,
    classification: str,
    lookback: int,
    markers: list[str],
) -> str | None:
    """Return a reason string when we predict codex would answer `skip`,
    otherwise None. Only fires for `input` classification — `menu` screens
    always carry numbered choices so they are answerable."""
    if classification != "input":
        return None
    nonempty = [l for l in clean_screen.splitlines() if l.strip()]
    if not nonempty:
        return "empty cleaned screen"
    window = nonempty[-max(lookback, 1):]
    haystack = "\n".join(window).lower()
    if any(m in haystack for m in markers):
        return None
    if any(NUMBERED_LIST_RE.match(l) for l in window):
        return None
    return f"no question marker in last {len(window)} non-empty line(s)"


def _screen_hash(clean_screen: str) -> str:
    return hashlib.sha256(clean_screen.encode("utf-8")).hexdigest()


def _cache_lookup(key: str, now: float) -> dict[str, Any] | None:
    entry = _DECISION_CACHE.get(key)
    if entry is None:
        return None
    expiry, decision = entry
    if now >= expiry:
        _DECISION_CACHE.pop(key, None)
        return None
    return decision


def _cache_store(key: str, decision: dict[str, Any], ttl: float, now: float) -> None:
    if ttl <= 0:
        return
    _DECISION_CACHE[key] = (now + ttl, decision)
    if len(_DECISION_CACHE) > _CACHE_MAX_ENTRIES:
        stale = [k for k, (exp, _) in _DECISION_CACHE.items() if exp <= now]
        for k in stale:
            _DECISION_CACHE.pop(k, None)


def _normalize_pane_id(raw: str) -> str:
    s = str(raw).strip()
    if not s:
        return ""
    return s if s.startswith("%") else f"%{s}"


def log_pane_allowed(pane_id: str, cfg: dict[str, Any]) -> bool:
    """Return True if per-pane audit / codex-out logging is permitted for
    this pane. Empty list (default) = all panes allowed."""
    allow = cfg.get("log_pane_ids") or []
    if not allow:
        return True
    wanted = {_normalize_pane_id(x) for x in allow}
    wanted.discard("")
    return _normalize_pane_id(pane_id) in wanted


# ---------- config & logging ---------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "poll_interval_seconds": 180,
    "stable_count_required": 2,
    "per_pane_cooldown_seconds": 15,
    "max_responses_per_window": 5,
    "response_window_minutes": 5,
    "codex_timeout_seconds": 90,
    "codex_binary": "codex",
    "capture_scrollback_lines": 200,
    "hr_min_length": 50,
    "socket_enabled": True,
    "socket_path": "",
    "log_enabled": False,
    "log_format": "%(asctime)s %(levelname)s %(message)s",
    "log_datefmt": "%Y-%m-%d %H:%M:%S",
    "log_max_bytes": 10_000_000,
    "log_backups": 3,
    "log_retention_days": 0,
    "log_pane_ids": [],
    "skip_predictor_enabled": True,
    "skip_predictor_lookback_lines": 15,
    "skip_predictor_extra_markers": [],
    "skip_decision_cache_ttl_seconds": 300,
}


def default_socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "default"
    base = runtime if runtime else "/tmp"
    return f"{base}/watcher-{user}.sock"


def resolve_socket_path(cfg: dict[str, Any]) -> str:
    return str(cfg["socket_path"]).strip() or default_socket_path()


def _coerce(default: Any, raw: str) -> Any:
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return raw


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            cfg.update(tomllib.load(f))
    for key, default in DEFAULTS.items():
        env_key = f"WATCHER_{key.upper()}"
        if env_key in os.environ:
            try:
                cfg[key] = _coerce(default, os.environ[env_key])
            except ValueError as e:
                raise SystemExit(f"invalid value for {env_key}: {e}") from e
    return cfg


def setup_logging(cfg: dict[str, Any]) -> None:
    fmt = logging.Formatter(cfg["log_format"], datefmt=cfg["log_datefmt"])
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stream)
    if not cfg["log_enabled"]:
        log.info("file logging disabled (log_enabled=false); audit trail skipped")
        return
    LOG_DIR.mkdir(mode=0o700, exist_ok=True)
    TRIGGER_DIR.mkdir(mode=0o700, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "watcher.log",
        maxBytes=int(cfg["log_max_bytes"]),
        backupCount=int(cfg["log_backups"]),
        encoding="utf-8",
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)


def prune_old_triggers(cfg: dict[str, Any]) -> int:
    if not cfg["log_enabled"]:
        return 0
    days = int(cfg["log_retention_days"])
    if days <= 0 or not TRIGGER_DIR.exists():
        return 0
    cutoff = time.time() - days * 86400
    pruned = 0
    for p in TRIGGER_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                pruned += 1
        except OSError as e:
            log.warning("prune failed for %s: %s", p, e)
    return pruned


# ---------- data classes -------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Pane:
    pane_id: str   # tmux #{pane_id}, e.g. "%3" — stable for pane lifetime
    target: str   # session:window.pane, valid for -t
    pid: int
    cmd: str
    title: str


@dataclasses.dataclass(slots=True)
class PaneState:
    history: deque[str] = dataclasses.field(default_factory=lambda: deque(maxlen=8))
    last_classification: str = "unknown"
    cooldown_until: float = 0.0
    responses_in_window: deque[float] = dataclasses.field(default_factory=deque)
    disabled: bool = False
    in_flight: bool = False
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)


# ---------- tmux helpers -------------------------------------------------------

SEP = "\t"  # Tab — tmux escapes control chars < 0x20 (except tab/newline) to literal "\nnn"

def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kw)


def discover_panes(my_pid: int) -> list[Pane]:
    fmt = SEP.join([
        "#{pane_id}",
        "#{session_name}:#{window_index}.#{pane_index}",
        "#{pane_pid}",
        "#{pane_current_command}",
        "#{pane_title}",
    ])
    try:
        out = _run(["tmux", "list-panes", "-a", "-F", fmt]).stdout
    except subprocess.CalledProcessError as e:
        log.warning("tmux list-panes failed: %s", e.stderr.strip())
        return []
    panes: list[Pane] = []
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) < 5:
            continue
        pane_id, target, pid_s, cmd, title = parts[0], parts[1], parts[2], parts[3], SEP.join(parts[4:])
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == my_pid:
            continue
        if cmd.strip() != "claude":
            continue
        panes.append(Pane(pane_id=pane_id, target=target, pid=pid, cmd=cmd, title=title))
    return panes


def capture_pane(target: str, scrollback: int = 200) -> str:
    try:
        out = _run(["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{scrollback}"]).stdout
    except subprocess.CalledProcessError as e:
        log.warning("capture-pane %s failed: %s", target, e.stderr.strip())
        return ""
    return out


# ---------- classification -----------------------------------------------------

def is_spinner_title(title: str) -> bool:
    s = title.strip()
    if not s:
        return False
    return 0x2800 <= ord(s[0]) <= 0x28FF


def is_working(screen: str, title: str) -> bool:
    if is_spinner_title(title):
        return True
    # Only check the bottom status bar — `(ctrl+o to expand)` legitimately
    # appears deeper in scrollback inside collapsed tool-output blocks
    # (`+N lines (ctrl+o to expand)`), which would falsely look like working.
    tail = "\n".join(screen.rstrip("\n").splitlines()[-5:])
    return any(h in tail for h in WORKING_HINTS)


def _has_empty_input_box(screen: str) -> bool:
    lines = screen.rstrip("\n").splitlines()
    n = len(lines)
    if n < 3:
        return False
    for i in range(n - 1, 1, -1):
        if not is_hr_line(lines[i]):
            continue
        j = i - 1
        while j > 0 and not lines[j].strip():
            j -= 1
        if not is_empty_prompt_line(lines[j]):
            continue
        k = j - 1
        while k > 0 and not lines[k].strip():
            k -= 1
        if k >= 0 and is_hr_line(lines[k]):
            return True
    return False


def has_queued_input(screen: str) -> bool:
    """User has typed a draft into the input box (Claude Code displays it
    below the box prefixed with `｜`). Auto-responder must not intervene.

    Custom statuslines (`📂` anchor) can also render lines starting with `｜`
    (e.g. multi-line stderr previews), which would falsely look like queued
    drafts. Queued drafts always appear *above* the statusline, so restrict
    the search window to lines preceding the first `📂` row in the tail.
    """
    lines = screen.splitlines()[-15:]
    statusline_start = next((i for i, l in enumerate(lines) if "📂" in l), len(lines))
    candidates = [l for l in lines[:statusline_start] if l.strip()]
    for l in candidates:
        if l.lstrip().startswith(QUEUE_MARKER):
            return True
    return False


def classify(screen: str, title: str) -> str:
    if is_working(screen, title):
        return "working"
    if has_queued_input(screen):
        return "drafting"
    tail_lines = screen.splitlines()[-30:]
    if any(MENU_CHOICE_RE.match(l) for l in tail_lines):
        return "menu"
    if _has_empty_input_box(screen):
        return "input"
    return "other"


# ---------- trigger gating -----------------------------------------------------

def should_trigger(
    state: PaneState,
    classification: str,
    cfg: dict[str, Any],
    require_stable: bool = True,
) -> bool:
    if state.disabled or state.in_flight:
        return False
    if time.time() < state.cooldown_until:
        return False
    if classification not in ("input", "menu"):
        return False
    if require_stable:
        need = int(cfg["stable_count_required"])
        if len(state.history) < need:
            return False
        recent = list(state.history)[-need:]
        if not all(x == recent[0] for x in recent):
            return False
    # rate-limit window
    now = time.time()
    cutoff = now - float(cfg["response_window_minutes"]) * 60.0
    while state.responses_in_window and state.responses_in_window[0] < cutoff:
        state.responses_in_window.popleft()
    if len(state.responses_in_window) >= int(cfg["max_responses_per_window"]):
        if not state.disabled:
            log.warning("killswitch tripped — pane disabled (>= %d responses in %.1f min)",
                        cfg["max_responses_per_window"], cfg["response_window_minutes"])
        state.disabled = True
        return False
    return True


def evaluate_pane(
    pane: Pane,
    state: PaneState,
    cfg: dict[str, Any],
    dry_run: bool,
    scrollback: int,
    from_hook: bool = False,
) -> bool:
    """Capture pane, classify, and schedule handle_pane if conditions met.
    Returns True if a handler was scheduled."""
    screen = capture_pane(pane.target, scrollback)
    if not screen:
        return False
    state.history.append(screen)
    classification = classify(screen, pane.title)
    state.last_classification = classification
    if not should_trigger(state, classification, cfg, require_stable=not from_hook):
        return False
    state.in_flight = True
    src = "hook" if from_hook else "poll"
    log.info("evaluate %s class=%s src=%s — scheduling handler", pane.target, classification, src)
    asyncio.create_task(handle_pane(pane, state, screen, cfg, dry_run))
    return True


# ---------- codex invocation ---------------------------------------------------

def build_prompt(screen: str) -> str:
    return PROMPT_TEMPLATE.format(screen=_clean_screen(screen))


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    s = text.strip()
    m = _CODE_FENCE_RE.match(s)
    return m.group(1).strip() if m else s


async def call_codex(pane: Pane, screen: str, cfg: dict[str, Any]) -> dict[str, Any]:
    ts = int(time.time())
    safe_id = pane.pane_id.lstrip("%")
    keep_log = bool(cfg["log_enabled"]) and log_pane_allowed(pane.pane_id, cfg)
    if keep_log:
        out_file = TRIGGER_DIR / f"codex-out-{ts}-{safe_id}.txt"
    else:
        fd, tmp_path = tempfile.mkstemp(prefix=f"codex-out-{ts}-{safe_id}-", suffix=".txt")
        os.close(fd)
        out_file = Path(tmp_path)
    full_prompt = build_prompt(screen)

    args = [
        cfg.get("codex_binary", "codex"), "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "-C", str(WATCHER_DIR),
        "--output-schema", str(SCHEMA_PATH),
        "--output-last-message", str(out_file),
        full_prompt,
    ]
    log.info("codex exec → %s (prompt %d chars)", pane.target, len(full_prompt))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timeout = float(cfg["codex_timeout_seconds"])
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            log.error("codex timed out after %.0fs for %s; terminating", timeout, pane.target)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            raise
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", "replace")[-600:]
            raise RuntimeError(f"codex exit {proc.returncode}: {tail}")
        raw = out_file.read_text(encoding="utf-8")
    finally:
        if not keep_log:
            out_file.unlink(missing_ok=True)
    cleaned = _strip_fences(raw)
    return json.loads(cleaned)


# ---------- action application -------------------------------------------------

async def apply_action(
    pane: Pane,
    action: str,
    value: str | None,
    baseline: str,
    scrollback: int,
) -> str:
    fresh = capture_pane(pane.target, scrollback)
    if fresh != baseline:
        return "aborted-pane-changed"
    if action == "skip":
        return f"skipped:{(value or '').strip()[:80]}"
    if action == "enter":
        _run(["tmux", "send-keys", "-t", pane.target, "Enter"])
        return "enter"
    if action == "key":
        v = (value or "").strip()
        if len(v) != 1 or not v.isdigit():
            return f"invalid-key:{v!r}"
        _run(["tmux", "send-keys", "-t", pane.target, v])
        await asyncio.sleep(0.15)
        _run(["tmux", "send-keys", "-t", pane.target, "Enter"])
        return f"key:{v}"
    if action == "text":
        if not value:
            return "empty-text"
        _run(["tmux", "send-keys", "-t", pane.target, "-l", value])
        await asyncio.sleep(0.15)
        _run(["tmux", "send-keys", "-t", pane.target, "Enter"])
        return f"text:{len(value)}chars"
    return f"unknown-action:{action!r}"


# ---------- audit log ----------------------------------------------------------

def audit(pane: Pane, snapshot: str, decision: dict[str, Any], outcome: str, cfg: dict[str, Any]) -> None:
    if not cfg["log_enabled"]:
        return
    if not log_pane_allowed(pane.pane_id, cfg):
        return
    record = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "pane_id": pane.pane_id,
        "target": pane.target,
        "title": pane.title,
        "outcome": outcome,
        "decision": decision,
        "snapshot": snapshot,
    }
    safe_id = pane.pane_id.lstrip("%")
    fn = TRIGGER_DIR / f"{int(time.time())}-{safe_id}.json"
    fn.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- per-pane handler ---------------------------------------------------

async def handle_pane(
    pane: Pane,
    state: PaneState,
    snapshot: str,
    cfg: dict[str, Any],
    dry_run: bool,
) -> None:
    try:
        async with state.lock:
            log.info("trigger %s class=%s title=%r", pane.target, state.last_classification, pane.title)

            # Pre-codex short-circuits: cache hit, then heuristic skip predictor.
            clean = _clean_screen(snapshot)
            shash = _screen_hash(clean)
            cache_ttl = float(cfg["skip_decision_cache_ttl_seconds"])
            cooldown = float(cfg["per_pane_cooldown_seconds"])
            pre_now = time.time()
            cached = _cache_lookup(shash, pre_now)
            if cached is not None:
                log.info("cache hit for %s: %s", pane.target, cached.get("action"))
                state.cooldown_until = pre_now + cooldown
                cached_decision = {**cached, "source": "cache"}
                try:
                    audit(pane, snapshot, cached_decision, "cached-skip", cfg)
                except Exception:
                    log.exception("audit write failed")
                return
            if bool(cfg["skip_predictor_enabled"]):
                reason = predict_skip(
                    clean,
                    state.last_classification,
                    int(cfg["skip_predictor_lookback_lines"]),
                    _build_markers(cfg),
                )
                if reason:
                    log.info("predicted skip for %s: %s", pane.target, reason)
                    state.cooldown_until = pre_now + cooldown
                    decision = {"action": "skip", "value": reason, "source": "predicted"}
                    _cache_store(shash, decision, cache_ttl, pre_now)
                    try:
                        audit(pane, snapshot, decision, "predicted-skip", cfg)
                    except Exception:
                        log.exception("audit write failed")
                    return

            try:
                decision = await call_codex(pane, snapshot, cfg)
            except Exception as e:
                log.error("codex call failed for %s: %s", pane.target, e)
                now = time.time()
                state.responses_in_window.append(now)
                state.cooldown_until = now + cooldown
                try:
                    audit(pane, snapshot, {"action": "error", "error": str(e)[:500]}, "codex-error", cfg)
                except Exception:
                    log.exception("audit write failed")
                return
            action = decision.get("action", "")
            value = decision.get("value")
            log.info("codex decision for %s: action=%s value=%r", pane.target, action, value)
            if action == "skip":
                _cache_store(shash, dict(decision), cache_ttl, time.time())
            if dry_run:
                outcome = f"dry-run:{action}:{(value or '')[:80]}"
            else:
                try:
                    outcome = await apply_action(
                        pane, action, value, baseline=snapshot,
                        scrollback=int(cfg["capture_scrollback_lines"]),
                    )
                except Exception as e:
                    log.exception("apply_action failed for %s", pane.target)
                    outcome = f"apply-error:{e}"
            log.info("outcome %s: %s", pane.target, outcome)
            now = time.time()
            state.responses_in_window.append(now)
            state.cooldown_until = now + float(cfg["per_pane_cooldown_seconds"])
            try:
                audit(pane, snapshot, decision, outcome, cfg)
            except Exception:
                log.exception("audit write failed")
    finally:
        state.in_flight = False


# ---------- hook socket server -------------------------------------------------

async def handle_hook_event(
    pane_id: str,
    states: dict[str, PaneState],
    cfg: dict[str, Any],
    dry_run: bool,
) -> None:
    """Stop-hook arrived. Find pane, capture once, evaluate with stable-check bypassed."""
    panes = discover_panes(os.getpid())
    pane = next((p for p in panes if p.pane_id == pane_id), None)
    if pane is None:
        log.info("hook event for unknown pane %s; ignored", pane_id)
        return
    state = states.setdefault(pane.pane_id, PaneState())
    evaluate_pane(pane, state, cfg, dry_run,
                  scrollback=int(cfg["capture_scrollback_lines"]),
                  from_hook=True)


async def start_socket_server(
    states: dict[str, PaneState],
    cfg: dict[str, Any],
    dry_run: bool,
) -> asyncio.AbstractServer | None:
    if not cfg["socket_enabled"]:
        log.info("hook socket disabled (socket_enabled=false)")
        return None
    sock_path = resolve_socket_path(cfg)
    path = Path(sock_path)
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            log.warning("could not remove stale socket %s: %s", path, e)
            return None
    path.parent.mkdir(parents=True, exist_ok=True)

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                return
            pane_id = data.decode("utf-8", "replace").strip()
            if not pane_id:
                return
            try:
                await handle_hook_event(pane_id, states, cfg, dry_run)
            except Exception:
                log.exception("hook handler failed for %s", pane_id)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_unix_server(on_connect, path=str(path))
    except OSError as e:
        log.warning("could not bind socket %s: %s — hook channel disabled", path, e)
        return None
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    log.info("hook socket listening at %s", path)
    return server


# ---------- main loop ----------------------------------------------------------

async def main_loop(
    states: dict[str, PaneState],
    cfg: dict[str, Any],
    dry_run: bool,
    stop: asyncio.Event,
) -> None:
    my_pid = os.getpid()
    interval = float(cfg["poll_interval_seconds"])
    scrollback = int(cfg["capture_scrollback_lines"])
    log.info("watcher poll loop (pid=%d, interval=%.1fs, dry_run=%s)", my_pid, interval, dry_run)

    pruned = prune_old_triggers(cfg)
    if pruned:
        log.info("startup prune: removed %d old trigger files", pruned)
    last_prune = time.time()
    prune_interval = 3600.0

    while not stop.is_set():
        try:
            panes = discover_panes(my_pid)
        except Exception:
            log.exception("discover_panes failed")
            panes = []

        active_ids = {p.pane_id for p in panes}
        for stale in list(states):
            if stale not in active_ids:
                states.pop(stale, None)

        for pane in panes:
            state = states.setdefault(pane.pane_id, PaneState())
            evaluate_pane(pane, state, cfg, dry_run, scrollback=scrollback, from_hook=False)

        if time.time() - last_prune > prune_interval:
            pruned = prune_old_triggers(cfg)
            if pruned:
                log.info("periodic prune: removed %d old trigger files", pruned)
            last_prune = time.time()

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    log.info("watcher poll loop exiting")


async def run_daemon(cfg: dict[str, Any], dry_run: bool) -> None:
    states: dict[str, PaneState] = {}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    server = await start_socket_server(states, cfg, dry_run)
    try:
        await main_loop(states, cfg, dry_run, stop)
    finally:
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
            try:
                Path(resolve_socket_path(cfg)).unlink(missing_ok=True)
            except OSError:
                pass
        log.info("watcher exiting")


async def run_once(cfg: dict[str, Any]) -> int:
    """Single-tick diagnostic: print discovered panes + classification, no codex."""
    panes = discover_panes(os.getpid())
    if not panes:
        print("no claude panes found")
        return 0
    scrollback = int(cfg["capture_scrollback_lines"])
    for p in panes:
        screen = capture_pane(p.target, scrollback)
        cls = classify(screen, p.title)
        print(f"{p.target}  pane={p.pane_id}  pid={p.pid}  class={cls}  title={p.title!r}")
    return 0


# ---------- CLI entrypoint -----------------------------------------------------

def cli_entry() -> None:
    ap = argparse.ArgumentParser(description="Auto-respond to Claude prompts via Codex CLI.")
    ap.add_argument("--once", action="store_true", help="single tick, print discovery, no codex call")
    ap.add_argument("--dry-run", action="store_true", help="full loop but skip send-keys")
    args = ap.parse_args()
    cfg = load_config()
    setup_logging(cfg)
    if args.once:
        sys.exit(asyncio.run(run_once(cfg)))
    try:
        asyncio.run(run_daemon(cfg, dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli_entry()
