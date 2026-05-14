"""Watcher daemon: monitor WezTerm panes running Claude Code on Windows, ask
Codex CLI what to reply, then send keys back via `wezterm cli send-text`.

Run: `uv run watcher.py`  (or `uv run watcher`)
     `uv run watcher.py --once`     → single discovery/classification tick, no codex
     `uv run watcher.py --dry-run`  → full loop but skip the final send-text
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
import shutil
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
  - When the same affirmative action is offered as both a one-time approval
    ("Yes", "Yes, proceed") and a permanent approval ("Yes, and don't ask
    again", "Always allow"), ALWAYS prefer the one-time option. Permanent
    approvals remove future checkpoints and are hard to reverse.
  - For a `❯` free-text input box, prefer action="text" with a concise reply
    that answers the visible question. If the latest question is a clear yes/no
    or one-word check, answer it directly.
  - For an `❯ 1.` style menu line, prefer action="key" with the matching digit.
  - Plan Mode confirmation (Claude proposes a multi-step plan and asks to
    proceed): if the visible plan content looks complete and reasonable,
    approve it. If the plan box appears truncated at the top (you can see a
    closing `╰` border without a matching `╭` opener), pick the "No, keep
    planning" option instead of approving blindly.

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


def log_pane_allowed(pane_id: int, cfg: dict[str, Any]) -> bool:
    """Return True if per-pane audit / codex-out logging is permitted for
    this pane. Empty list (default) = all panes allowed. WezTerm pane IDs
    are integers; legacy string forms (`"%18"` or `"18"`) are accepted in
    config and coerced via int(), trailing `%` stripped for compatibility.
    """
    allow = cfg.get("log_pane_ids") or []
    if not allow:
        return True
    wanted: set[int] = set()
    for x in allow:
        s = str(x).strip().lstrip("%")
        if not s:
            continue
        try:
            wanted.add(int(s))
        except ValueError:
            continue
    return pane_id in wanted


# ---------- config & logging ---------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "poll_interval_seconds": 180,
    "stable_count_required": 2,
    "per_pane_cooldown_seconds": 15,
    "max_responses_per_window": 5,
    "response_window_minutes": 5,
    "codex_timeout_seconds": 90,
    "codex_binary": "codex",
    "capture_scrollback_lines": 100,
    "capture_escalation_step": 400,
    "max_capture_scrollback_lines": 2000,
    "prompt_context_lines": 60,
    "hr_min_length": 50,
    "socket_enabled": True,
    "socket_host": "127.0.0.1",
    "socket_port": 47823,
    "socket_token": "",
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
    "milestone_toggle_state_path": "",
    "milestone_command_text": "/milestone-runner",
    "milestone_max_reinjects_per_window": 5,
    "milestone_reinject_cooldown_seconds": 5,
}


def socket_info_path() -> Path:
    """Per-user file recording the TCP host/port the daemon is listening on.
    Stop-hook reads this to find a running daemon without hardcoding the port.
    """
    return Path.home() / ".watcher" / "socket-info.json"


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
    pane_id: int      # WezTerm pane id (integer, stable for pane lifetime)
    window_id: int    # WezTerm window id
    tab_id: int       # WezTerm tab id
    workspace: str    # WezTerm workspace name (often empty)
    title: str        # WezTerm pane title (foreground process / shell hint)


@dataclasses.dataclass(slots=True)
class PaneState:
    history: deque[str] = dataclasses.field(default_factory=lambda: deque(maxlen=8))
    last_classification: str = "unknown"
    cooldown_until: float = 0.0
    responses_in_window: deque[float] = dataclasses.field(default_factory=deque)
    disabled: bool = False
    in_flight: bool = False
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)


# ---------- wezterm helpers ----------------------------------------------------
#
# WezTerm replaces tmux as the terminal multiplexer. Pane IDs are integers
# (not `%N` strings); there is no `pane_current_command` field, so we drop
# the `cmd == "claude"` pre-filter and let classify() do the heavy lifting
# via visual fingerprints (NBSP `❯`, HR lines with embedded session label,
# Braille spinner). False positives in pane discovery are filtered out by
# classify() returning "other"; cost of capturing all panes is negligible
# (poll_interval default 180s × ~20 panes = <0.12 wezterm calls/sec).

WEZTERM_BIN = "wezterm"


def _wezterm_run(args: list[str], timeout: float = 5.0,
                 stdin_input: str | None = None) -> str:
    """Invoke `wezterm cli <args>`, return stdout. Raises CalledProcessError
    on non-zero exit. Callers MUST wrap to graceful failure (no crash).
    """
    return subprocess.run(
        [WEZTERM_BIN, "cli", *args],
        capture_output=True, text=True, check=True, timeout=timeout,
        input=stdin_input,
    ).stdout


def wezterm_list_panes() -> list[dict[str, Any]]:
    """Return raw pane list from `wezterm cli list --format json`, or [] on
    any failure (GUI not running, JSON malformed, executable missing).
    """
    try:
        raw = _wezterm_run(["list", "--format", "json"], timeout=3.0)
        data = json.loads(raw)
        if not isinstance(data, list):
            log.warning("wezterm cli list returned non-list: %r", type(data))
            return []
        return data
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("wezterm cli list failed: %s", e)
        return []


def wezterm_get_text(pane_id: int, scrollback: int = 100) -> str:
    """Capture pane content as plain text (no ANSI escapes). WezTerm's
    `--start-line -N` requests up to N lines from above the visible screen;
    Claude Code uses alt-screen so scrollback past the viewport is generally
    unavailable, but the viewport (~40-50 lines) already contains everything
    classify() needs.
    """
    try:
        # --escapes preserves SGR; we want them for _strip_ghost_text() to
        # detect dim/reverse-video attributes on autocomplete suggestions.
        return _strip_ghost_text(_wezterm_run(
            ["get-text", "--pane-id", str(pane_id),
             "--escapes",
             "--start-line", f"-{max(scrollback, 1)}"],
            timeout=3.0,
        ))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as e:
        log.warning("wezterm cli get-text pane=%d failed: %s", pane_id, e)
        return ""


def wezterm_send_text(pane_id: int, payload: str) -> None:
    """Send raw text to a pane WITHOUT bracketed-paste wrapping. Embed `\\r`
    inside `payload` to fire Enter as part of the same call — verified in
    Phase 1 spike (R2) that Claude Code TUI treats this as native keystrokes
    rather than paste data.
    """
    subprocess.run(
        [WEZTERM_BIN, "cli", "send-text", "--no-paste",
         "--pane-id", str(pane_id)],
        input=payload, text=True, check=True, timeout=3.0,
    )


def wezterm_pane_exists(pane_id: int) -> bool:
    """Cheap existence check for a pane (used to clean up stale state)."""
    return any(int(p.get("pane_id", -1)) == pane_id for p in wezterm_list_panes())


def discover_panes(_my_pid: int) -> list[Pane]:
    """Enumerate every WezTerm pane. `_my_pid` retained for signature
    compatibility (used to skip the daemon's own pane in the tmux era)
    but no longer needed — wezterm cli list never includes the daemon's
    own python process anyway.
    """
    _ = _my_pid  # kept for compatibility
    panes: list[Pane] = []
    for entry in wezterm_list_panes():
        try:
            panes.append(Pane(
                pane_id=int(entry["pane_id"]),
                window_id=int(entry.get("window_id", 0)),
                tab_id=int(entry.get("tab_id", 0)),
                workspace=str(entry.get("workspace", "")),
                title=str(entry.get("title", "")),
            ))
        except (KeyError, ValueError, TypeError) as e:
            log.warning("malformed pane entry %r: %s", entry, e)
            continue
    return panes


_ANSI_CSI_RE = re.compile(r"\x1b\[([0-9:;?]*)([a-zA-Z])")
# OSC: ESC ] ... terminated by BEL (0x07) or ST (ESC \).
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# DCS / SOS / PM / APC: ESC (P|X|^|_) ... ST.
_ANSI_STR_RE = re.compile(r"\x1b[PX^_][^\x1b]*\x1b\\")
# Charset designators: ESC ( B, ESC ) 0, etc. (G0..G3 select).
_CHARSET_INTRO = "()*+-./"


def _strip_ghost_text(raw: str) -> str:
    """Remove characters rendered with SGR dim (\\e[2m) or reverse-video
    (\\e[7m) attributes, then strip all remaining ANSI escapes. Claude Code
    paints autocomplete/ghost-text suggestions in dim and parks the cursor on
    the first suggested character with reverse-video — both indistinguishable
    from real input once SGR codes are stripped. Dropping these chars makes a
    ghosted `❯ <suggestion>` line collapse back to an empty prompt so
    `classify()` sees the true idle state.

    Also strips non-CSI escapes: ISO-2022 charset designators (`ESC ( B`,
    `ESC ) 0` etc), OSC strings (`ESC ] ... BEL`), DCS/SOS/PM/APC strings,
    and single-byte Fe sequences. WezTerm emits `\\x1b(B` when switching
    between ASCII and DEC line-drawing for box borders; leaving these in
    the cleaned screen pollutes codex's view with `(B` litter.

    Side effect: a real cursor reverse-video block (almost always sitting on
    the trailing whitespace after typed input) also gets dropped — harmless
    because typed input is the non-reversed prefix.
    """
    out: list[str] = []
    dim = False
    reverse = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\x1b" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "[":
                m = _ANSI_CSI_RE.match(raw, i)
                if m:
                    params, final = m.group(1), m.group(2)
                    if final == "m":
                        parts = params.split(";") if params else ["0"]
                        for p in parts:
                            p = p or "0"
                            if p == "0":
                                dim = False
                                reverse = False
                            elif p == "2":
                                dim = True
                            elif p == "22":
                                dim = False
                            elif p == "7":
                                reverse = True
                            elif p == "27":
                                reverse = False
                    i = m.end()
                    continue
            elif nxt == "]":
                m = _ANSI_OSC_RE.match(raw, i)
                if m:
                    i = m.end()
                    continue
            elif nxt in "PX^_":
                m = _ANSI_STR_RE.match(raw, i)
                if m:
                    i = m.end()
                    continue
            elif nxt in _CHARSET_INTRO and i + 2 < n:
                i += 3  # ESC + intro + charset id
                continue
            else:
                # Unrecognized 2-byte Fe sequence (ESC D, ESC E, ESC H,
                # ESC M, ESC N, ESC O, ESC 7, ESC 8, ESC =, ESC >, ESC c…).
                i += 2
                continue
        if ch == "\x1b":
            i += 1
            continue
        if dim or reverse:
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def capture_pane(pane_id: int, scrollback: int = 100) -> str:
    """Compatibility shim — delegates to wezterm_get_text. Kept to minimize
    diff in capture_pane_adaptive() and apply_action()."""
    return wezterm_get_text(pane_id, scrollback)


_TRUNCATION_BOTTOM_WINDOW = 15  # how far up from the bottom we still treat a `╰` as "current"


def is_capture_truncated(screen: str) -> bool:
    """Heuristic: Claude Code draws bordered boxes for plan confirmations,
    permission dialogs, and other multi-line prompts. Top border opens with
    `╭`, bottom closes with `╰`. The current prompt's `╰` always sits near
    the bottom (the menu/input lines beneath it are short). Look for a `╰`
    in the last `_TRUNCATION_BOTTOM_WINDOW` lines only; older completed
    boxes higher up are ignored on purpose (dragging more history just
    pollutes codex's view of the current decision). If that bottom `╰` has
    no matching `╭` anywhere in the capture, the current box is cut off →
    escalate scrollback.
    """
    lines = screen.splitlines()
    n = len(lines)
    bottom_floor = max(0, n - _TRUNCATION_BOTTOM_WINDOW)
    last_close = -1
    for i in range(n - 1, bottom_floor - 1, -1):
        if "╰" in lines[i]:
            last_close = i
            break
    if last_close < 0:
        return False
    for j in range(last_close - 1, -1, -1):
        if "╭" in lines[j]:
            return False
    return True


def _trim_to_current_decision(screen: str, fallback_lines: int) -> str:
    """Reduce the screen to only the current decision's context before it
    is sent to codex. Two cases:

    1. The current prompt is wrapped in a bordered box (plan confirmation,
       permission dialog, etc.) whose `╰` sits within the bottom window:
       keep from that box's matching `╭` downward. Everything above is
       older conversation and gets dropped.
    2. No current-decision box detected (plain input box, bare numbered
       menu, or any older `╰` higher up): keep only the last
       `fallback_lines` lines — enough to include Claude's most recent
       question without dragging in earlier turns.
    """
    lines = screen.splitlines()
    n = len(lines)
    bottom_floor = max(0, n - _TRUNCATION_BOTTOM_WINDOW)
    last_close = -1
    for i in range(n - 1, bottom_floor - 1, -1):
        if "╰" in lines[i]:
            last_close = i
            break
    if last_close >= 0:
        for j in range(last_close - 1, -1, -1):
            if "╭" in lines[j]:
                return "\n".join(lines[j:])
    if fallback_lines <= 0 or n <= fallback_lines:
        return "\n".join(lines)
    return "\n".join(lines[-fallback_lines:])


def capture_pane_adaptive(pane_id: int, cfg: dict[str, Any]) -> str:
    """Capture pane content, escalating scrollback if a bordered box is
    detected as truncated above the captured window. Stops when no longer
    truncated, when scrollback hits max, or when a larger capture returns
    identical content (wezterm alt-screen tends to cap out quickly).
    """
    scrollback = int(cfg["capture_scrollback_lines"])
    max_sb = int(cfg["max_capture_scrollback_lines"])
    step = int(cfg["capture_escalation_step"])
    screen = capture_pane(pane_id, scrollback)
    if not screen:
        return screen
    while is_capture_truncated(screen) and scrollback < max_sb:
        next_sb = min(scrollback + step, max_sb)
        bigger = capture_pane(pane_id, next_sb)
        if not bigger or bigger == screen:
            break
        log.info("escalated scrollback %d → %d for pane=%d (truncated box)",
                 scrollback, next_sb, pane_id)
        screen = bigger
        scrollback = next_sb
    return screen


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
    from_hook: bool = False,
) -> bool:
    """Capture pane, classify, and schedule handle_pane if conditions met.
    Returns True if a handler was scheduled."""
    screen = capture_pane_adaptive(pane.pane_id, cfg)
    if not screen:
        return False
    state.history.append(screen)
    classification = classify(screen, pane.title)
    state.last_classification = classification
    if not should_trigger(state, classification, cfg, require_stable=not from_hook):
        return False
    state.in_flight = True
    src = "hook" if from_hook else "poll"
    log.info("evaluate pane=%d class=%s src=%s — scheduling handler", pane.pane_id, classification, src)
    asyncio.create_task(handle_pane(pane, state, screen, cfg, dry_run))
    return True


# ---------- codex invocation ---------------------------------------------------

def build_prompt(screen: str, cfg: dict[str, Any]) -> str:
    cleaned = _clean_screen(screen)
    fallback = int(cfg.get("prompt_context_lines", 60))
    trimmed = _trim_to_current_decision(cleaned, fallback)
    return PROMPT_TEMPLATE.format(screen=trimmed)


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    s = text.strip()
    m = _CODE_FENCE_RE.match(s)
    return m.group(1).strip() if m else s


async def call_codex(pane: Pane, full_prompt: str, cfg: dict[str, Any]) -> dict[str, Any]:
    ts = int(time.time())
    safe_id = pane.pane_id
    keep_log = bool(cfg["log_enabled"]) and log_pane_allowed(pane.pane_id, cfg)
    if keep_log:
        out_file = TRIGGER_DIR / f"codex-out-{ts}-{safe_id}.txt"
    else:
        fd, tmp_path = tempfile.mkstemp(prefix=f"codex-out-{ts}-{safe_id}-", suffix=".txt")
        os.close(fd)
        out_file = Path(tmp_path)

    # codex 0.130 on Windows: `-C C:\...` with backslashes parses fine when the
    # value is quoted as a single arg, but forward-slash form is portable and
    # avoids any future PATH parsing oddities — convert eagerly.
    cwd_arg = str(WATCHER_DIR).replace("\\", "/")
    # Windows: asyncio.create_subprocess_exec calls CreateProcessW which does
    # NOT consult PATHEXT, so a bare `codex` misses `codex.cmd`/`codex.bat`
    # shims (npm/yarn install codex CLI as `.cmd` on Windows). shutil.which()
    # walks PATHEXT and returns the resolved path. POSIX falls through fine.
    codex_bin = cfg.get("codex_binary", "codex")
    resolved = shutil.which(codex_bin) or codex_bin
    # Pass prompt via stdin (using `-` as positional) instead of CLI arg.
    # Windows: codex CLI ships as `codex.cmd` shim → spawned via cmd.exe /c,
    # which re-parses args and interprets `<`, `>`, `|`, `&`, `%`, `^` as
    # metacharacters. The prompt contains literal `<reply text>` and `> ` etc,
    # so cmd would mangle it before codex sees it. stdin path bypasses cmd
    # entirely. POSIX behaves identically — codex docs: "If not provided as
    # an argument (or if `-` is used), instructions are read from stdin".
    args = [
        resolved, "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "-C", cwd_arg,
        "--output-schema", str(SCHEMA_PATH),
        "--output-last-message", str(out_file),
        "-",
    ]
    log.info("codex exec → pane=%d (prompt %d chars)", pane.pane_id, len(full_prompt))
    # start_new_session=True is POSIX-only (calls setsid). On Windows use
    # CREATE_NEW_PROCESS_GROUP via creationflags so terminate() sends CTRL_BREAK
    # to codex without affecting the parent daemon's console.
    subprocess_kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        subprocess_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        subprocess_kwargs["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(*args, **subprocess_kwargs)
        timeout = float(cfg["codex_timeout_seconds"])
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.error("codex timed out after %.0fs for pane=%d; terminating", timeout, pane.pane_id)
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
    fresh = capture_pane(pane.pane_id, scrollback)
    if fresh != baseline:
        return "aborted-pane-changed"
    if action == "skip":
        return f"skipped:{(value or '').strip()[:80]}"
    # Phase 1 spike R2 verified: `wezterm cli send-text --no-paste` with `\r`
    # appended to the payload fires Enter in a single call (no second
    # send-text needed, unlike tmux which required separate -l and Enter).
    if action == "enter":
        wezterm_send_text(pane.pane_id, "\r")
        return "enter"
    if action == "key":
        v = (value or "").strip()
        if len(v) != 1 or not v.isdigit():
            return f"invalid-key:{v!r}"
        wezterm_send_text(pane.pane_id, v + "\r")
        return f"key:{v}"
    if action == "text":
        if not value:
            return "empty-text"
        # Drop any embedded \r/\n in the codex-supplied value to avoid
        # accidentally firing extra Enters mid-text — terminal control chars
        # in arbitrary LLM output are a footgun.
        sanitized = value.replace("\r", "").replace("\n", " ")
        wezterm_send_text(pane.pane_id, sanitized + "\r")
        return f"text:{len(sanitized)}chars"
    return f"unknown-action:{action!r}"


# ---------- audit log ----------------------------------------------------------

def audit(
    pane: Pane,
    snapshot: str,
    decision: dict[str, Any],
    outcome: str,
    cfg: dict[str, Any],
    codex_prompt: str | None = None,
) -> None:
    if not cfg["log_enabled"]:
        return
    if not log_pane_allowed(pane.pane_id, cfg):
        return
    record = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "pane_id": pane.pane_id,
        "window_id": pane.window_id,
        "tab_id": pane.tab_id,
        "workspace": pane.workspace,
        "title": pane.title,
        "outcome": outcome,
        "decision": decision,
        "snapshot": snapshot,
        "codex_prompt": codex_prompt,
    }
    fn = TRIGGER_DIR / f"{int(time.time())}-{pane.pane_id}.json"
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
            log.info("trigger %s class=%s title=%r", pane.pane_id, state.last_classification, pane.title)

            # Pre-codex short-circuits: cache hit, then heuristic skip predictor.
            clean = _clean_screen(snapshot)
            shash = _screen_hash(clean)
            cache_ttl = float(cfg["skip_decision_cache_ttl_seconds"])
            cooldown = float(cfg["per_pane_cooldown_seconds"])
            pre_now = time.time()
            cached = _cache_lookup(shash, pre_now)
            if cached is not None:
                log.info("cache hit for %s: %s", pane.pane_id, cached.get("action"))
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
                    log.info("predicted skip for %s: %s", pane.pane_id, reason)
                    state.cooldown_until = pre_now + cooldown
                    decision = {"action": "skip", "value": reason, "source": "predicted"}
                    _cache_store(shash, decision, cache_ttl, pre_now)
                    try:
                        audit(pane, snapshot, decision, "predicted-skip", cfg)
                    except Exception:
                        log.exception("audit write failed")
                    return

            codex_prompt = build_prompt(snapshot, cfg)
            try:
                decision = await call_codex(pane, codex_prompt, cfg)
            except Exception as e:
                log.error("codex call failed for %s: %s", pane.pane_id, e)
                now = time.time()
                state.responses_in_window.append(now)
                state.cooldown_until = now + cooldown
                try:
                    audit(
                        pane, snapshot,
                        {"action": "error", "error": str(e)[:500]},
                        "codex-error", cfg, codex_prompt=codex_prompt,
                    )
                except Exception:
                    log.exception("audit write failed")
                return
            action = decision.get("action", "")
            value = decision.get("value")
            log.info("codex decision for %s: action=%s value=%r", pane.pane_id, action, value)
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
                    log.exception("apply_action failed for %s", pane.pane_id)
                    outcome = f"apply-error:{e}"
            log.info("outcome %s: %s", pane.pane_id, outcome)
            now = time.time()
            state.responses_in_window.append(now)
            state.cooldown_until = now + float(cfg["per_pane_cooldown_seconds"])
            try:
                audit(pane, snapshot, decision, outcome, cfg, codex_prompt=codex_prompt)
            except Exception:
                log.exception("audit write failed")
    finally:
        state.in_flight = False


# ---------- milestone-toggle bridge --------------------------------------------

def _toggle_state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    if base:
        return Path(base) / "watcher"
    return Path.home() / ".local" / "state" / "watcher"


def resolve_toggle_state_path(cfg: dict[str, Any]) -> Path:
    override = str(cfg.get("milestone_toggle_state_path", "")).strip()
    if override:
        return Path(override).expanduser()
    return _toggle_state_root() / "milestone-toggle.json"


def resolve_completion_marker_dir(cfg: dict[str, Any]) -> Path:
    return _toggle_state_root() / "milestone-done"


def read_toggle_state(cfg: dict[str, Any]) -> dict[str, Any]:
    path = resolve_toggle_state_path(cfg)
    if not path.exists():
        return {"version": 1, "panes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("milestone-toggle state unreadable %s: %s", path, e)
        return {"version": 1, "panes": {}}
    if not isinstance(data, dict):
        return {"version": 1, "panes": {}}
    data.setdefault("version", 1)
    if not isinstance(data.get("panes"), dict):
        data["panes"] = {}
    return data


def write_toggle_state(data: dict[str, Any], cfg: dict[str, Any]) -> None:
    path = resolve_toggle_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def completion_marker_fresh(pane_id: int, entry: dict[str, Any], cfg: dict[str, Any]) -> bool:
    marker = resolve_completion_marker_dir(cfg) / f"{pane_id}.json"
    if not marker.exists():
        return False
    enabled_at = entry.get("enabled_at", "")
    try:
        enabled_ts = dt.datetime.fromisoformat(str(enabled_at)).timestamp()
    except (TypeError, ValueError):
        return False
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return False
    return mtime > enabled_ts


def _milestone_rate_limit_ok(entry: dict[str, Any], cfg: dict[str, Any]) -> bool:
    window_min = float(cfg.get("response_window_minutes", 5))
    max_n = int(cfg.get("milestone_max_reinjects_per_window", 5))
    now = time.time()
    cutoff = now - window_min * 60.0
    raw = entry.get("reinjects") or []
    fresh = [t for t in raw if isinstance(t, (int, float)) and t >= cutoff]
    entry["reinjects"] = fresh
    return len(fresh) < max_n


def _pane_window_tab(pane: Pane) -> str:
    """WezTerm equivalent of tmux's `session:window.pane` location string —
    used as the milestone-toggle `session_window` value to detect when a
    pane is moved between windows/tabs and auto-disable the milestone.
    """
    return f"{pane.window_id}:{pane.tab_id}"


async def handle_milestone_event(
    pane_id: int,
    states: dict[int, PaneState],
    cfg: dict[str, Any],
    dry_run: bool,
) -> None:
    """Toggle ON for this pane. Re-inject the milestone command unless the
    completion marker says we are done, the pane is busy, or rate-limit trips."""
    state_data = read_toggle_state(cfg)
    entry = state_data.get("panes", {}).get(str(pane_id))
    if not entry or not entry.get("enabled"):
        return
    if completion_marker_fresh(pane_id, entry, cfg):
        entry["enabled"] = False
        entry["completed_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        write_toggle_state(state_data, cfg)
        log.info("milestone-toggle auto-off pane=%d (completion marker)", pane_id)
        return
    panes = discover_panes(os.getpid())
    pane = next((p for p in panes if p.pane_id == pane_id), None)
    if pane is None:
        log.info("milestone-toggle: pane %d not found; skipping", pane_id)
        return
    expected_window = entry.get("session_window")
    current_window = _pane_window_tab(pane)
    if expected_window and current_window != expected_window:
        log.warning("milestone-toggle: pane %d window changed (%s -> %s); auto-off",
                    pane_id, expected_window, current_window)
        entry["enabled"] = False
        write_toggle_state(state_data, cfg)
        return
    state = states.setdefault(pane_id, PaneState())
    screen = capture_pane_adaptive(pane.pane_id, cfg)
    if not screen:
        return
    cls = classify(screen, pane.title)
    if cls in ("working", "drafting"):
        log.info("milestone-toggle: pane %d busy (class=%s); skipping inject", pane_id, cls)
        return
    if not _milestone_rate_limit_ok(entry, cfg):
        log.warning("milestone-toggle killswitch pane=%d (>= %d injects in %.1f min)",
                    pane_id,
                    int(cfg["milestone_max_reinjects_per_window"]),
                    float(cfg["response_window_minutes"]))
        entry["enabled"] = False
        write_toggle_state(state_data, cfg)
        return
    cmd = str(cfg.get("milestone_command_text") or "/milestone-runner")
    if dry_run:
        log.info("milestone-toggle: dry-run inject %r into pane=%d", cmd, pane.pane_id)
        entry.setdefault("reinjects", []).append(time.time())
        write_toggle_state(state_data, cfg)
        return
    try:
        wezterm_send_text(pane.pane_id, cmd + "\r")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError) as e:
        log.warning("milestone-toggle: send-text failed for pane=%d: %s",
                    pane.pane_id, e)
        return
    state.cooldown_until = time.time() + float(cfg["milestone_reinject_cooldown_seconds"])
    entry.setdefault("reinjects", []).append(time.time())
    write_toggle_state(state_data, cfg)
    log.info("milestone-toggle: injected %r into pane=%d", cmd, pane.pane_id)


# ---------- hook socket server -------------------------------------------------

async def handle_hook_event(
    pane_id: int,
    states: dict[int, PaneState],
    cfg: dict[str, Any],
    dry_run: bool,
) -> None:
    """Stop-hook arrived. Find pane, capture once, evaluate with stable-check bypassed."""
    log.info("hook recv pane=%d", pane_id)
    panes = discover_panes(os.getpid())
    pane = next((p for p in panes if p.pane_id == pane_id), None)
    if pane is None:
        log.info("hook event for unknown pane %d; ignored", pane_id)
        return
    state = states.setdefault(pane.pane_id, PaneState())
    evaluate_pane(pane, state, cfg, dry_run, from_hook=True)


async def start_socket_server(
    states: dict[int, PaneState],
    cfg: dict[str, Any],
    dry_run: bool,
) -> asyncio.AbstractServer | None:
    """TCP loopback hook channel. Replaces the unix-socket transport that
    only worked on POSIX. Bind is restricted to 127.0.0.1 to keep traffic
    on the host's loopback interface; the connection handler additionally
    rejects any non-loopback peer as defense-in-depth. Port is configurable
    with auto-retry (+0..+10) when the preferred port is occupied.
    """
    if not cfg["socket_enabled"]:
        log.info("hook socket disabled (socket_enabled=false)")
        return None

    host = str(cfg["socket_host"]).strip() or "127.0.0.1"
    want_port = int(cfg["socket_port"])
    bound_port: int | None = None
    server: asyncio.AbstractServer | None = None
    last_err: OSError | None = None

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = writer.get_extra_info("peername")
            if peer and peer[0] not in ("127.0.0.1", "::1"):
                log.warning("rejecting non-loopback peer: %s", peer)
                return
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                return
            line = data.decode("utf-8", "replace").strip()
            if not line:
                return
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    msg = {"event": "stop", "pane": line}
            except json.JSONDecodeError:
                msg = {"event": "stop", "pane": line}

            want_token = str(cfg.get("socket_token", "")).strip()
            got_token = str(msg.get("token", "")).strip()
            if want_token and got_token != want_token:
                log.warning("rejecting hook payload with token mismatch from %s", peer)
                return

            raw_pane = msg.get("pane")
            try:
                pane_id = int(str(raw_pane).strip().lstrip("%"))
            except (TypeError, ValueError):
                log.warning("invalid pane in hook payload: %r", raw_pane)
                return

            try:
                state_data = read_toggle_state(cfg)
                entry = state_data.get("panes", {}).get(str(pane_id))
                if entry and entry.get("enabled"):
                    await handle_milestone_event(pane_id, states, cfg, dry_run)
                else:
                    await handle_hook_event(pane_id, states, cfg, dry_run)
            except Exception:
                log.exception("hook handler failed for pane=%d", pane_id)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    for offset in range(11):
        candidate = want_port + offset
        try:
            server = await asyncio.start_server(on_connect, host=host, port=candidate)
            bound_port = candidate
            break
        except OSError as e:
            last_err = e
            continue

    if server is None or bound_port is None:
        log.error("TCP hook server failed to bind %s:%d..%d: %s",
                  host, want_port, want_port + 10, last_err)
        return None

    info = {
        "host": host,
        "port": bound_port,
        "pid": os.getpid(),
        "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "token_required": bool(str(cfg.get("socket_token", "")).strip()),
    }
    info_path = socket_info_path()
    try:
        info_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = info_path.with_suffix(info_path.suffix + ".tmp")
        tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
        os.replace(tmp, info_path)
    except OSError as e:
        log.warning("could not write socket-info file %s: %s", info_path, e)

    log.info("TCP hook server listening on %s:%d", host, bound_port)
    return server


# ---------- main loop ----------------------------------------------------------

async def main_loop(
    states: dict[int, PaneState],
    cfg: dict[str, Any],
    dry_run: bool,
    stop: asyncio.Event,
) -> None:
    my_pid = os.getpid()
    interval = float(cfg["poll_interval_seconds"])
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
            evaluate_pane(pane, state, cfg, dry_run, from_hook=False)

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
    states: dict[int, PaneState] = {}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # loop.add_signal_handler is POSIX-only — on Windows ProactorEventLoop it
    # raises NotImplementedError. Fall back to letting KeyboardInterrupt
    # propagate from asyncio.run (Ctrl+C still works via Windows' default
    # signal delivery, just without the in-loop graceful flag).
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError, RuntimeError):
            pass

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
        # Clear socket-info regardless of whether server was up — stale info
        # would cause hooks to connect-refused after daemon exit.
        try:
            socket_info_path().unlink(missing_ok=True)
        except OSError:
            pass
        log.info("watcher exiting")


async def run_once(cfg: dict[str, Any]) -> int:
    """Single-tick diagnostic: print discovered panes + classification, no codex."""
    panes = discover_panes(os.getpid())
    if not panes:
        print("no wezterm panes found (is WezTerm GUI running?)")
        return 0
    for p in panes:
        screen = capture_pane_adaptive(p.pane_id, cfg)
        cls = classify(screen, p.title)
        print(f"pane={p.pane_id:<4} window={p.window_id} tab={p.tab_id} "
              f"class={cls:<8} title={p.title!r}")
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
