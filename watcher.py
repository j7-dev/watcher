"""Watcher daemon: monitor WezTerm panes running Claude Code on Windows, ask
Codex CLI what to reply, then send keys back via `wezterm cli send-text`.

Run: `uv run watcher.py`  (or `uv run watcher`)
     `uv run watcher.py --once`     → single tick: classify + invoke codex for every input/menu pane, then exit
     `uv run watcher.py --once --dry-run` → same as --once but skip send-text
     `uv run watcher.py --dry-run`  → full daemon loop but skip the final send-text
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
你是 Claude Code 終端 pane 的自動回應決策代理人。每次呼叫你會收到一張
靜態畫面快照，pane 已閒置數秒。你的任務：以符合 schema 的 JSON 回一個
action。

═══ 三條根本約束 ═══
1. **無前後記憶**：除下方畫面與可選的 session 脈絡外，你不知道任何事；
   不可腦補。
2. **螢幕為唯一真實**：畫面看不到的就是不知道；session 脈絡只能用於
   消歧義（例如理解使用者意圖），與畫面衝突時以畫面為準，不可蓋過畫面。
3. **行動代價不對稱**：錯 `skip` 讓使用者下輪自處理（小代價）；錯
   `text`/`key` 可能觸發不可逆操作（大代價）。模糊時偏 skip，
   **但畫面明顯有等待答案的問題、或 Claude 陳述了下一步打算做什麼時，
   請優先主動回覆（給答案或推進）而非 skip。**

═══ 自動推進原則（narrative / 陳述句也要動作） ═══
Claude 的輸出常常是**陳述句而非問句**——它會總結成果、列舉文件、宣告
下一步計畫（"下一步建議：..."、"接下來會..."、"完整文件：..."）。
**這類陳述句 user 通常期望 daemon 主動推進**，不要被動 `skip idle`。

判斷流程：
- 陳述句 + 暗示有下一步可做（"下一步...", "接下來...", "Week N 用..."）
  → `text`，value=簡短中性推進語（如 "繼續"、"好"、"請開始"、"OK 開始"）。
- 陳述句 + 只是工作報告 / 無明確下一步（純成果總結、文件清單、task tick list）
  → `text`，value=「繼續」推 Claude 自行決定下一步。
- **安全閘**：陳述句裡若提到不可逆關鍵字（刪除 / drop / rm / force / 砍掉 /
  清空 / DROP TABLE / reset --hard / 強推），改 `skip` value=`dangerous-narrative`。
- 真正純閒置（畫面空、無內容、無 Claude 輸出）→ `skip` value=`idle`。

推進語規則：
- **中性**：不假設細節（不指定 file 名 / 數字 / 路徑），只說「繼續」「好」。
- **短**：≤ 6 字最佳，最多 12 字。
- **語言匹配**：畫面中文 → 中文推進語；畫面英文 → 英文（"continue" / "go"）。
- **絕對不可用 text 在已填 `❯ <已有文字>` 場景**——那會 append 串成亂碼。

═══ 符號圖例（畫面已預處理，請依此語意解讀） ═══
- `> <文字>`   → conversation 內**已 submit 的歷史 user 訊息**（不可變、僅供脈絡，
  不代表正在等待輸入）。**絕對不可**因為看到 `>` 開頭的長字串就判 user-mid-compose。
- `❯ <文字>`   → 底部 input box cursor + **未送出的草稿**（罕見；可能是上輪
  自動化已 type 但 Enter 未送的 stale state）。
- `❯ <數字>.` → 編號選單 cursor，顯示目前 default 選項。
- `<數字>. <文字>` / `<數字>) <文字>`（line-leading **裸數字**，無 `❯` 前綴）
  → conversation 內文的編號列表 / 條列說明，**不是** 互動 menu。即使該行
  含 `?`，那也是內文的 rhetorical question 而非等 user 作答。**不可送
  `key` / `enter`**——畫面只是列舉/描述，不需 user 動作。
- 空 input box（`❯ ` + 空白 + 兩條水平線包夾）已被預處理移除，畫面看不到時
  代表 input box 是空的、等待新輸入。
- 開頭出現 `[…上文略]` 表示畫面已從中段截斷，**不可**用「畫面看不到 X」
  反推「X 不存在」；對被截斷的脈絡保持保守。

═══ 5W1H 推理流程（內部依序回答後才產 action） ═══
- **What**：畫面尾段屬哪一類？編號選單 / 自由文字輸入框 / 純訊息 / 純閒置。
- **Who**：提問者是 Claude Code TUI（真實問題），不是 user 打字中。
- **When**：狀態新鮮嗎？畫面無 `❯` 結尾（空 input box，新鮮）vs `❯ <已有文字>`
  （可能上輪自動化已 type 但 Enter 未送的 stale state）。
- **Where**：游標 `❯` 落在哪一行？編號選項上、還是 input box 草稿上？
- **Why**：往上掃畫面，提示在問什麼或在告知什麼？三類：
    a) **徵詢核准**（"要繼續嗎？"、"是否進行？"、"Y/n?"、"Press 1..."、
       "請選擇..."）→ 真問句，判 `enter` / `key` / `text` 給答案。
    b) **自宣告下一步 / 工作陳述**（"下一步建議：..."、"接下來會..."、
       "完整文件：..."、task tick list、輸出總結）→ 陳述句，依「自動
       推進原則」判 `text` 主動推進（短中性推進語）。
    c) **純閒置**（畫面空、無 Claude 內容）→ `skip` value=`idle`。
- **How**：對照下方決策表選 action。

═══ Action schema（value 必填，不適用填 null） ═══
- `text`  value=<回覆文字>     → 輸入自由文字並 Enter
- `key`   value="1" | "2" | … → 按單一數字選編號
- `enter` value=null           → 僅按 Enter（接受游標所在預設）
- `skip`  value=<原因短句>     → 不動作

═══ 決策表（5W1H 分析後對照） ═══
- 空 `❯ ` + 上方有明確問題 → `text`，精簡回答（yes/no 或單詞就好）。
- 已填 `❯ <已有文字>`：
    - 文字合理回答了上方問題 → `enter`（接受已 type 內容；預設偏這個）。
    - 文字明顯是 user 草稿或答非所問 → `skip`，value=`user-mid-compose`
      或 `filled-input-mismatch`。
    - ⚠️ **不可用 `text`**——send-text 是 append 不是 replace，會串成亂碼。
- `❯ 1.` 編號選單：
    - 游標 `❯` 已在你要選的項上 → `enter`（最不易誤觸）。
    - 要切非游標項 → `key` value="<該數字>"。
- 純訊息 / Claude narrative / 陳述下一步 → 依「自動推進原則」判 `text`，
  value=中性推進語（"繼續" / "好" / "OK" / "continue"）。
- 純閒置（畫面真的沒內容） → `skip` value=`idle`。
- 含不可逆關鍵字的 narrative → `skip` value=`dangerous-narrative`。

═══ 安全閘（任一命中 → skip） ═══
1. 提示要求畫面拿不到的機密（密碼／API key／個資）。
2. 做錯會不可逆遺失工作（rm -rf、force-push、DROP TABLE、
   dirty tree 上 git reset --hard、刪除有未合併 commit 的分支）。
3. 畫面沒有實際問題或選單，只是閒置。

═══ 選擇偏好（多個合理答案時的偏序） ═══
- **提權 vs fallback**：選項給「sudo／系統安裝／全域狀態」與「純軟體
  fallback」二擇一時，**選 fallback**，除非畫面明示提權必要。
- **一次性 vs 永久**：同一肯定動作有「Yes」與「Yes, don't ask again
  ／Always allow」兩版時，**選永久核准**——信任自動化、避免重複被問。
- **計畫被截斷**：Plan Mode 可見 `╰` 收尾卻看不到對應 `╭` 起頭 →
  「No, keep planning」而非盲核准。

═══ Session 脈絡使用規則（若下方提供 Session 脈絡區段） ═══
脈絡僅供消歧義（例：判斷 user 是否在 mid-compose、編號選單該選哪項
最符合使用者意圖）。脈絡可能 stale（已換主題、清過 history）；與畫面
不一致時直接忽略脈絡，不可因脈絡偏離畫面內容。

{session_context}--- 畫面擷取（介於圍欄之間） ---
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
    # 標點
    "?", "？",

    # 英文詢問句型
    "do you", "would you", "want me to", "would you like",
    "shall i", "shall we", "should i", "may i",
    "let me know", "tell me",
    "which", "prefer",

    # 英文確認/執行
    "continue", "confirm", "proceed", "approve",
    "ready", "sound good", "ok?", "okay?",

    # 英文選擇/輸入提示
    "press", "choose", "select", "pick", "option",
    "y/n", "yes/no", "(y/n)",
    "reply with",

    # 中文詢問
    "是否", "要不要", "要嗎",
    "請選", "請輸入", "請問", "請告知",
    "還是", "哪個", "哪一個", "哪種",
    "想要", "偏好", "比較喜歡",

    # 中文確認
    "確認", "同意", "好嗎", "可以嗎", "行嗎",
    "怎麼樣", "如何",
    "告訴我", "讓我知道",
    "繼續嗎", "開始嗎",
    "選哪", "選擇",

    # 下一步
    "下一", "接下來", "後續", "for future", "remaining", "剩下", "剩餘", "next round", "follow-up", "follow up"
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
# fullwidth `｜` (U+FF5C). They are stripped from the codex prompt by
# `_strip_input_box_tail` so codex decides off the visible question/menu only;
# classify() no longer short-circuits on them — earlier behaviour suppressed
# real menu / input prompts when drafts were also visible.
QUEUE_MARKER = "｜"

# ---------- chrome filter for codex prompt ------------------------------------
# Strip pane chrome that adds noise without information (welcome banner, status
# line, footer hints, spinner counters) so codex sees only the conversation.
_WELCOME_MARKERS = ("Welcome back", "Tips for getting started", "/release-notes for more")
_STATUSLINE_RE = re.compile(r"📂")               # status line: model badge/dir/branch (📂 is unique anchor)
_FOOTER_RE     = re.compile(r"^\s*⏵⏵\s+(bypass|auto-accept)")  # bottom mode hint
_BAKED_RE      = re.compile(r"^\s*\S\s+\S.*?(?:[…\.]+\s*\(\d|\s+for\s+\d+\s*[ms])")  # spinner status: "✻ Sautéed for 6m 53s" / "✶ Nebulizing… (24m 54s · ...)" / "✻ Scaffolding monorepo root… (1h 2m 44s · ...)"
_TOKEN_RE      = re.compile(                                    # token counter / `/clear` hint line (right-aligned status)
    r"^\s*"
    r"(?:new task\?\s+/clear to save\s+)?"   # optional contextual hint prefix
    r"\d+(?:\.\d+)?[kKmM]?\s+tokens\s*$"     # `114738 tokens`, `225.3k tokens`, `1.2M tokens`
)
_RECAP_RE      = re.compile(r"^\s*※\s*recap", re.IGNORECASE)    # Claude self-injected `※ recap: ...` summary block


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
    in_recap = False
    for l in lines[start:]:
        # `※ recap: ...` is Claude's self-injected context summary block,
        # spanning the recap line + any indented continuation until a blank
        # line. Drop the whole block so codex isn't double-fed context (the
        # daemon already passes session meta separately).
        if not in_recap and _RECAP_RE.match(l):
            in_recap = True
            continue
        if in_recap:
            if not l.strip():
                in_recap = False
            continue
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
    default_lc = [m.lower() for m in DEFAULT_QUESTION_MARKERS]
    return [*default_lc, *extra_lc]


def predict_skip(
    clean_screen: str,
    classification: str,
    lookback: int,
    markers: list[str],
) -> str | None:
    """Return a reason string when we predict codex would answer `skip`,
    otherwise None. Only applies to `input` classification (menus always
    have numbered options worth presenting to codex).

    Short-circuits to skip when the cleaned screen's last `lookback`
    nonempty lines contain NEITHER any `markers` substring (case-insensitive)
    NOR a numbered list line (`1.` / `1)` style). Markers are matched
    case-insensitively: caller is expected to lowercase them (see
    `_build_markers`), and we lowercase the screen tail here."""
    if classification != "input":
        return None
    nonempty = [l for l in clean_screen.splitlines() if l.strip()]
    if not nonempty:
        return "empty cleaned screen"
    tail = nonempty[-lookback:] if lookback and lookback > 0 else nonempty
    tail_text = "\n".join(tail).lower()
    if any(m in tail_text for m in markers):
        return None
    if any(NUMBERED_LIST_RE.match(l) for l in tail):
        return None
    return "no question markers in lookback"


def _screen_hash(clean_screen: str, context: str = "") -> str:
    """Hash key for the decision cache. Optional `context` (e.g. user's current
    Claude Code prompt) is mixed in so identical screens belonging to different
    user requests do not share a skip decision — the codex answer for the same
    visual prompt can legitimately differ when the user's intent differs.
    Empty context preserves the original screen-only hash for pre-session-meta
    callers / polling-only panes."""
    h = hashlib.sha256()
    h.update(clean_screen.encode("utf-8"))
    if context:
        h.update(b"\x1f")  # ASCII unit separator — safe delimiter, never in text
        h.update(context.encode("utf-8"))
    return h.hexdigest()


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
    # Rate-limit handling: detect "You've hit your limit" prompt, press Enter
    # to accept the highlighted "Stop and wait" option, then schedule a delayed
    # resume that types `rate_limit_resume_text` after the reset time passes.
    "rate_limit_detection_enabled": True,
    "rate_limit_resume_text": "繼續",
    "rate_limit_resume_buffer_seconds": 10,
    "rate_limit_max_wait_hours": 26,
    "rate_limit_tz_fallback": "Asia/Taipei",
}


def socket_info_path() -> Path:
    """Per-user file recording the TCP host/port the daemon is listening on.
    Stop-hook reads this to find a running daemon without hardcoding the port.
    """
    return Path.home() / ".watcher" / "socket-info.json"


# ---------- singleton enforcement ---------------------------------------------
#
# Two-layer guard prevents a second `uv run watcher.py` (foreground OR
# daemon-launched) from racing the first one:
#
#   1. File lock on ~/.watcher/watcher.lock — OS-level mandatory mutex
#      (msvcrt.locking LK_NBLCK on Windows, fcntl.flock LOCK_EX|LOCK_NB on
#      POSIX). Released automatically when the process exits, including
#      crashes — no stale-lock recovery problem.
#   2. PID + started_at + exe path verification against socket-info.json —
#      defense-in-depth catching the (rare) case where the kernel released
#      the lock but the old process is still alive. Also produces a useful
#      diagnostic message pointing at the offending PID.

_LOCK_FD: int | None = None  # keep alive for the lifetime of this process


def watcher_lock_path() -> Path:
    return Path.home() / ".watcher" / "watcher.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_exe(pid: int) -> str | None:
    """Best-effort: return the executable path of pid, or None on failure.
    Used for PID-reuse defence — if PID is reused by a non-watcher process,
    we want to overwrite the stale socket-info, not refuse to start."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(size)
            )
            return buf.value if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def _read_singleton_info() -> dict[str, Any]:
    info_path = socket_info_path()
    if not info_path.exists():
        return {}
    try:
        return json.loads(info_path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _is_watcher_pid(pid: int) -> bool:
    """Check pid is alive AND its executable looks like a python interpreter.
    Conservative: on lookup failure, return True (treat as same-watcher) so we
    err on the side of refusing — a missed kill is recoverable, a duplicate
    daemon causing send-text races is not."""
    if not _pid_alive(pid):
        return False
    exe = _pid_exe(pid)
    if exe is None:
        return True  # can't tell → assume same watcher, refuse to start
    return "python" in exe.lower() or "pythonw" in exe.lower()


def acquire_singleton_lock() -> None:
    """Acquire process-wide exclusive lock or exit(2). Must be called BEFORE
    starting the socket server / poll loop. Lock is held for the lifetime of
    this process via the module-global fd; the OS releases it on exit."""
    global _LOCK_FD
    lock_path = watcher_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        info = _read_singleton_info()
        pid = int(info.get("pid", 0) or 0)
        started = str(info.get("started_at", "") or "")
        alive = _pid_alive(pid) if pid else False
        msg = (
            "watcher already running"
            + (f" (pid={pid}" if pid else "")
            + (f", started_at={started}" if started else "")
            + (f", alive={alive}" if pid else "")
            + (")" if pid else "")
            + f". Refusing to start a second instance. Lock: {lock_path}\n"
        )
        sys.stderr.write(msg)
        sys.exit(2)

    # Defense in depth: lock acquired but socket-info points at a still-alive
    # python process from a previous incarnation (kernel released the lock
    # without that process exiting — should not happen, but if it does, the
    # old one will fight us over send-text). Verify and refuse.
    info = _read_singleton_info()
    pid = int(info.get("pid", 0) or 0)
    if pid and pid != os.getpid() and _is_watcher_pid(pid):
        started = str(info.get("started_at", "") or "")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        sys.stderr.write(
            f"watcher already running per socket-info (pid={pid}"
            + (f", started_at={started}" if started else "")
            + "). Refusing to start a second instance.\n"
        )
        sys.exit(2)

    _LOCK_FD = fd  # keep fd open for process lifetime; OS releases on exit


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
    last_classification: str = "unknown"
    cooldown_until: float = 0.0
    responses_in_window: deque[float] = dataclasses.field(default_factory=deque)
    disabled: bool = False
    in_flight: bool = False
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    # Latest Claude Code transcript path learned from a Stop-hook stdin
    # payload. Used by extract_session_meta() to enrich the codex prompt
    # with ai-title / last-prompt context. Polling-only panes (no hook
    # event ever received) leave this None and run with screen-only prompts.
    # Daemon restart loses this — re-populated on the next Stop hook.
    transcript_path: str | None = None
    # Active rate-limit resume task. When the pane shows "You've hit your
    # limit · resets <time>", the detector parses the reset time, Enter-confirms
    # the highlighted "Stop and wait" option, and schedules a task to send the
    # resume text (config: rate_limit_resume_text) once the reset time passes.
    # `rate_limit_resume_at` is the epoch the task will fire at — used to
    # dedupe re-detections of the same limit screen.
    rate_limit_task: asyncio.Task[None] | None = None
    rate_limit_resume_at: float = 0.0


# ---------- wezterm helpers ----------------------------------------------------
#
# WezTerm replaces tmux as the terminal multiplexer. Pane IDs are integers
# (not `%N` strings); there is no `pane_current_command` field, so we drop
# the `cmd == "claude"` pre-filter and let classify() do the heavy lifting
# via visual fingerprints (NBSP `❯`, HR lines with embedded session label,
# `esc to interrupt` footer). False positives in pane discovery are filtered
# out by classify() returning "other"; cost of capturing all panes is negligible
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


_CONV_USER_PREFIX_RE = re.compile(r"^(\s*)❯\s+(\S.*)$")


def _rewrite_conversation_user_prefix(text: str) -> str:
    r"""Rewrite line-leading `❯ <text>` in the conversation region to `> <text>`.

    Claude Code TUI uses `❯` for three distinct things: (a) the bottom input
    box cursor (`❯ ` + NBSP), (b) the menu cursor (`❯ 1. Yes`), and (c) the
    prefix Claude renders for already-submitted user messages in the
    conversation scrollback. Codex cannot distinguish (a) from (c) when the
    bottom input box is stripped, leading to false `skip user-mid-compose`
    when conversation history starts with a long `❯ ...` line.

    Strategy: locate the bottom input-box region (HR + empty `❯` / queued
    drafts + HR) and rewrite `❯ <text>` to `> <text>` ONLY above that region.
    Menu cursor lines (`❯ \d+[.)]`) are preserved so action `key` / `enter`
    decisions still work. The bottom input box itself is left untouched so
    `_strip_input_box_tail` can remove it downstream.
    """
    lines = text.splitlines()
    n = len(lines)
    box_start = n

    i = n - 1
    while i >= 0:
        s = lines[i].strip()
        if not s or s.startswith(QUEUE_MARKER):
            i -= 1
            continue
        break

    if i >= 0 and is_hr_line(lines[i]):
        # Walk up through interior (anything that isn't another HR). If the
        # interior contains at least one `❯` line (empty, placeholder, or
        # drafted), treat the HR-HR sandwich as the input-box region so the
        # cursor `❯` semantics inside the box are preserved.
        j = i - 1
        found_prompt = False
        while j >= 0 and not is_hr_line(lines[j]):
            if lines[j].lstrip().startswith("❯"):
                found_prompt = True
            j -= 1
        if j >= 0 and is_hr_line(lines[j]) and found_prompt:
            box_start = j

    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx >= box_start:
            out.append(line)
            continue
        m = _CONV_USER_PREFIX_RE.match(line)
        if not m:
            out.append(line)
            continue
        indent, rest = m.group(1), m.group(2)
        if NUMBERED_LIST_RE.match(rest):
            out.append(line)
            continue
        out.append(f"{indent}> {rest}")
    return "\n".join(out)


def _strip_input_box_tail(text: str) -> str:
    """Drop the trailing empty input box and any queued drafts. Codex only
    needs the conversation tail (Claude's last question/output) to decide;
    the empty `❯` prompt and `｜`-prefixed queued drafts are noise.

    Conservative pattern (bottom-up):
      1. drop trailing blank lines and `｜` queued-draft lines
      2. if bottom is HR, peek up: tentatively eat HR-bottom, then any
         interior of only-blank-or-empty-prompt lines, then expect HR-top
      3. only strip the whole box when both HRs found AND interior had no
         substantive content (so menu boxes with `❯ 1.` survive intact)
    """
    lines = text.splitlines()
    n = len(lines)

    i = n - 1
    while i >= 0:
        s = lines[i].strip()
        if not s or s.startswith(QUEUE_MARKER):
            i -= 1
            continue
        break

    if i < 0:
        return ""

    if is_hr_line(lines[i]):
        j = i - 1
        while j >= 0:
            s = lines[j].strip()
            if not s or is_empty_prompt_line(lines[j]):
                j -= 1
                continue
            break
        if j >= 0 and is_hr_line(lines[j]):
            return "\n".join(lines[:j]).rstrip("\n")

    return "\n".join(lines[: i + 1]).rstrip("\n")


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

def is_working(screen: str) -> bool:
    # Only check the bottom status bar — `(ctrl+o to expand)` legitimately
    # appears deeper in scrollback inside collapsed tool-output blocks
    # (`+N lines (ctrl+o to expand)`), which would falsely look like working.
    tail = "\n".join(screen.rstrip("\n").splitlines()[-5:])
    return any(h in tail for h in WORKING_HINTS)


def _has_input_box(screen: str) -> bool:
    """Detect an `❯ ...` input row sandwiched between two HR lines — accept
    both empty `❯ ` (waiting for user) and filled `❯ <text>` (text already
    typed but not submitted, e.g. orphaned from a prior failed `text` action).
    The queue lines below the box start with `｜`, not `❯`, so they don't
    match. codex sees the filled text and decides enter (submit) vs text
    (replace) vs skip (user clearly mid-composing)."""
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
        if not lines[j].lstrip().startswith("❯"):
            continue
        k = j - 1
        while k > 0 and not lines[k].strip():
            k -= 1
        if k >= 0 and is_hr_line(lines[k]):
            return True
    return False


def classify(screen: str) -> str:
    if is_working(screen):
        return "working"
    tail_lines = screen.splitlines()[-30:]
    if any(MENU_CHOICE_RE.match(l) for l in tail_lines):
        return "menu"
    if _has_input_box(screen):
        return "input"
    return "other"


# ---------- trigger gating -----------------------------------------------------

def should_trigger(
    state: PaneState,
    classification: str,
    cfg: dict[str, Any],
) -> bool:
    if state.disabled or state.in_flight:
        return False
    if time.time() < state.cooldown_until:
        return False
    if classification not in ("input", "menu"):
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
        log.debug("evaluate pane=%d capture empty", pane.pane_id)
        return False
    classification = classify(screen)
    state.last_classification = classification
    src = "hook" if from_hook else "poll"
    if not should_trigger(state, classification, cfg):
        # Log skipped panes at INFO so missing/non-triggering panes are
        # observable without enabling DEBUG. Cheap (<200 panes typical) and
        # essential for diagnosing "why didn't pane N fire" complaints.
        now = time.time()
        cd_left = max(0.0, state.cooldown_until - now)
        log.info(
            "evaluate pane=%d class=%s src=%s — skip (disabled=%s in_flight=%s cd_left=%.1fs win=%d)",
            pane.pane_id, classification, src,
            state.disabled, state.in_flight, cd_left, len(state.responses_in_window),
        )
        return False
    state.in_flight = True
    log.info("evaluate pane=%d class=%s src=%s — scheduling handler", pane.pane_id, classification, src)
    asyncio.create_task(handle_pane(pane, state, screen, cfg, dry_run))
    return True


# ---------- session metadata extraction ----------------------------------------
#
# Claude Code writes one JSONL line per turn into
#   ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
# alongside the rolling conversation. Three lightweight metadata entry types
# carry user intent in a few hundred bytes (vs raw turns which run into 100K+
# tokens) and are safe to surface to codex as disambiguation context:
#
#   {"type":"ai-title",    "aiTitle":"<one-line topic>",      ...}
#   {"type":"last-prompt", "lastPrompt":"<user's current msg>", ...}
#   {"type":"summary",     "summary":"<post-/compact summary>", ...}   (rare)
#
# `ai-title` and `last-prompt` are rewritten every turn; the first `last-prompt`
# in the file is therefore the user's initial request, the final one is the
# current request. Forward-scan O(n) is acceptable — files rarely exceed a few
# MB. Failure modes are all "return None" — codex still works on screen alone.

_MAX_META_FIELD = 800  # per-field char cap to bound prompt size & redact long pastes


def extract_session_meta(transcript_path: str | None) -> dict[str, str] | None:
    """Best-effort extraction of lightweight Claude Code session metadata.

    Returns a dict with string fields (any may be empty):
        title             — latest ai-title.aiTitle (one-line topic, AI-named)
        initial_request   — first  last-prompt.lastPrompt
        current_request   — latest last-prompt.lastPrompt
        summary           — latest summary.summary (only after /compact)

    Returns None when the path is missing/unreadable/empty of metadata.
    """
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.is_file():
        return None
    title = ""
    initial_request = ""
    current_request = ""
    summary = ""
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                if t == "ai-title":
                    v = d.get("aiTitle")
                    if isinstance(v, str) and v.strip():
                        title = v.strip()
                elif t == "last-prompt":
                    v = d.get("lastPrompt")
                    if isinstance(v, str) and v.strip():
                        s = v.strip()
                        if not initial_request:
                            initial_request = s
                        current_request = s
                elif t == "summary":
                    v = d.get("summary")
                    if isinstance(v, str) and v.strip():
                        summary = v.strip()
    except OSError as e:
        log.warning("extract_session_meta: %s unreadable: %s", p, e)
        return None
    if not (title or initial_request or current_request or summary):
        return None
    return {
        "title": title[:_MAX_META_FIELD],
        "initial_request": initial_request[:_MAX_META_FIELD],
        "current_request": current_request[:_MAX_META_FIELD],
        "summary": summary[:_MAX_META_FIELD],
    }


def _format_session_context(meta: dict[str, str] | None) -> str:
    """Render meta dict into the `{session_context}` placeholder for
    PROMPT_TEMPLATE. Empty string means no context block — the template's
    surrounding usage-rule paragraph still renders but is a no-op for codex.
    """
    if not meta:
        return ""
    parts = ["═══ Session 脈絡（參考用，畫面仍為唯一真實） ═══"]
    if meta.get("title"):
        parts.append(f"主題：{meta['title']}")
    initial = meta.get("initial_request", "")
    current = meta.get("current_request", "")
    if initial:
        parts.append(f"最初需求：{initial}")
    if current and current != initial:
        parts.append(f"當前需求：{current}")
    if meta.get("summary"):
        parts.append(f"摘要：{meta['summary']}")
    return "\n".join(parts) + "\n\n"


# ---------- codex invocation ---------------------------------------------------

def build_prompt(
    screen: str,
    cfg: dict[str, Any],
    session_meta: dict[str, str] | None = None,
) -> str:
    cleaned = _clean_screen(screen)
    fallback = int(cfg.get("prompt_context_lines", 60))
    trimmed = _trim_to_current_decision(cleaned, fallback)
    # If trimming dropped leading context (both `╭` head-detect and
    # fallback last-N paths keep the tail), prepend a marker so codex
    # knows the head was sliced — prevents "I don't see X above ⇒ X
    # doesn't exist" false reasoning.
    if trimmed and trimmed != cleaned and cleaned.endswith(trimmed):
        trimmed = "[…上文略]\n" + trimmed
    rewritten = _rewrite_conversation_user_prefix(trimmed)
    stripped = _strip_input_box_tail(rewritten)
    return PROMPT_TEMPLATE.format(
        screen=stripped,
        session_context=_format_session_context(session_meta),
    )


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
    # Compare chrome-stripped screens so the always-ticking spinner counter line
    # (`✶ Nebulizing… (52m 24s · …)`) and other status-bar churn don't cause
    # false-positive aborts during the 5s codex round-trip. Real content edits
    # (user typed something, menu changed, new prompt) survive `_clean_screen`.
    if _clean_screen(fresh) != _clean_screen(baseline):
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
        # Two-phase send: typed text first, then Enter as a separate PTY write.
        # Combining `text + \r` in one buffer makes Claude Code's input handler
        # treat the burst as paste-like and the trailing CR becomes an in-input
        # newline rather than a submit. Both calls keep `--no-paste`, so no
        # bracketed-paste markers wrap either chunk — the TUI sees the second
        # write as a clean Enter keystroke after the typing has settled.
        wezterm_send_text(pane.pane_id, sanitized)
        await asyncio.sleep(0.15)
        wezterm_send_text(pane.pane_id, "\r")
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


# ---------- rate-limit detection & resume scheduling ---------------------------
#
# Claude Code shows a rate-limit prompt like
#
#     You've hit your limit · resets May 16, 1am (Asia/Taipei)
#
#     ❯ 1. Stop and wait
#       2. Switch model
#       3. ...
#
# The detector recognizes the limit phrase + numbered menu, presses Enter to
# accept the highlighted default ("Stop and wait" — option 1), and schedules a
# one-shot asyncio task that re-captures the pane after the reset time + a
# safety buffer and sends `rate_limit_resume_text` (default "繼續") so the
# session picks back up automatically.
#
# Rate-limit handling bypasses the codex round-trip entirely (deterministic
# signal) and does NOT count toward the per-pane killswitch — being throttled
# is a system response, not a codex action.

RATE_LIMIT_PHRASE_RE = re.compile(r"You['’]ve hit your limit", re.IGNORECASE)

# Reset clause variants we recognize. Ordered specific → general; first match
# wins. tz capture is optional — falls back to `rate_limit_tz_fallback`.
_RESET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "May 16, 1am" / "May 16 1:30pm" / "Sep 3, 11 pm"
    re.compile(
        r"resets\s+(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})[,\s]+"
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
        r"(?:\s*\((?P<tz>[\w/_+\-]+)\))?",
        re.IGNORECASE,
    ),
    # "Tomorrow at 1am" / "tomorrow 1:30am"
    re.compile(
        r"resets\s+tomorrow(?:\s+at)?\s+"
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
        r"(?:\s*\((?P<tz>[\w/_+\-]+)\))?",
        re.IGNORECASE,
    ),
    # "at 1am" / "1pm" (bare time — assume today; if past, roll to tomorrow)
    re.compile(
        r"resets\s+(?:at\s+)?"
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
        r"(?:\s*\((?P<tz>[\w/_+\-]+)\))?",
        re.IGNORECASE,
    ),
)


def _resolve_tz(name: str) -> Any:
    """Return a zoneinfo.ZoneInfo for `name`, or local tzinfo on failure.
    Imported lazily so a missing tzdata package on Windows can't break import."""
    if not name:
        return dt.datetime.now().astimezone().tzinfo
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except Exception:
        return dt.datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return dt.datetime.now().astimezone().tzinfo


def _ampm_to_24h(hour: int, ampm: str) -> int:
    a = ampm.lower()
    if a == "am":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


_MONTH_ABBR_TO_NUM: dict[str, int] = {
    m.lower(): i for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
    )
}


def _parse_reset_time(screen: str, tz_fallback: str) -> float | None:
    """Parse the `resets …` clause from the screen and return an absolute
    epoch (seconds), or None if no recognized clause / parse failure."""
    fallback_tz = _resolve_tz(tz_fallback)
    if fallback_tz is None:
        return None
    now = dt.datetime.now(tz=fallback_tz)
    for idx, pat in enumerate(_RESET_PATTERNS):
        m = pat.search(screen)
        if not m:
            continue
        g = m.groupdict()
        tz = _resolve_tz(g.get("tz") or tz_fallback) or fallback_tz
        try:
            hour = _ampm_to_24h(int(g["hour"]), g["ampm"])
        except (TypeError, ValueError):
            continue
        if not (0 <= hour <= 23):
            continue
        minute = int(g.get("minute") or 0)
        if not (0 <= minute <= 59):
            continue
        try:
            if idx == 0:  # explicit "<Month> <Day>"
                month_key = g["month"][:3].lower()
                month_num = _MONTH_ABBR_TO_NUM.get(month_key)
                if month_num is None:
                    continue
                day = int(g["day"])
                # Try current year first; if result is in the past, try +1y.
                for delta_year in (0, 1):
                    try:
                        candidate = dt.datetime(
                            now.year + delta_year, month_num, day,
                            hour, minute, tzinfo=tz,
                        )
                    except ValueError:
                        candidate = None
                    if candidate is not None and candidate > now:
                        return candidate.timestamp()
                continue
            if idx == 1:  # "tomorrow"
                d = (now + dt.timedelta(days=1)).date()
                return dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).timestamp()
            # bare time form ("resets 1:10am"). Claude Code shows this without
            # an explicit date and does NOT auto-refresh after the reset, so by
            # the time the daemon polls we may already be past the reset
            # instant. Two cases:
            #   (a) parsed time is in the future today  → use it as-is
            #   (b) parsed time is in the past today    → reset already happened;
            #       treat epoch as "now" so the resume task fires immediately
            #       (capped at -12h: anything further back is almost certainly
            #       a stale screen for *yesterday's* limit — roll to tomorrow).
            d = now.date()
            candidate = dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)
            if candidate > now:
                return candidate.timestamp()
            if (now - candidate).total_seconds() <= 12 * 3600:
                return now.timestamp()
            return (candidate + dt.timedelta(days=1)).timestamp()
        except Exception:
            log.exception("_parse_reset_time: unexpected error on pattern %d", idx)
            continue
    return None


def _find_queued_drafts(screen: str) -> list[str]:
    """Return list of queued draft message texts visible below the input box.
    Claude Code TUI renders queued drafts as lines starting with `｜<text>`
    (U+FF5C fullwidth vertical bar). Trailing spaces are stripped; empty/
    whitespace-only drafts are skipped."""
    out: list[str] = []
    for line in screen.splitlines():
        s = line.lstrip()
        if not s.startswith(QUEUE_MARKER):
            continue
        body = s[len(QUEUE_MARKER):].rstrip()
        if body:
            out.append(body)
    return out


def detect_rate_limit(screen: str, cfg: dict[str, Any]) -> float | None:
    """Return the reset epoch when `screen` shows a Claude Code rate-limit
    prompt with a parseable reset time AND a numbered menu in the tail.
    Returns None otherwise (caller falls through to the regular codex path).
    """
    if not bool(cfg.get("rate_limit_detection_enabled", True)):
        return None
    if not RATE_LIMIT_PHRASE_RE.search(screen):
        return None
    # Note: real Claude Code rate-limit screens render as a tool-result chunk
    # (`⎿ You've hit your limit · resets …`) above an empty input box — there
    # is NO numbered menu cursor (`❯ 1.`) to Enter-confirm. Detection only
    # requires the phrase + a parseable reset time; the resume task types the
    # configured text once the reset epoch passes.
    epoch = _parse_reset_time(screen, str(cfg.get("rate_limit_tz_fallback", "Asia/Taipei")))
    if epoch is None:
        return None
    now = time.time()
    max_wait_s = float(cfg.get("rate_limit_max_wait_hours", 26)) * 3600.0
    if epoch - now > max_wait_s:
        log.warning("rate-limit reset %.0f is %.1fh away (cap %.1fh); not scheduling",
                    epoch, (epoch - now) / 3600.0, max_wait_s / 3600.0)
        return None
    if epoch - now < -300.0:
        log.warning("rate-limit reset %.0f is in the past (>5min); not scheduling", epoch)
        return None
    return epoch


async def _resume_after_rate_limit(
    pane: Pane,
    state: PaneState,
    reset_epoch: float,
    cfg: dict[str, Any],
    dry_run: bool,
) -> None:
    """Sleep until `reset_epoch` + buffer, then re-capture the pane and send
    the resume text if it now looks ready (idle input box). If Claude TUI is
    still on the limit screen (clock skew, slow rollover), retry in 30s.
    If the pane has moved on to a different context, abort silently."""
    try:
        buffer_s = float(cfg.get("rate_limit_resume_buffer_seconds", 60))
        delay = max(0.0, reset_epoch + buffer_s - time.time())
        log.info(
            "rate-limit resume scheduled pane=%d reset_epoch=%.0f wait=%.1fs",
            pane.pane_id, reset_epoch, delay,
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            log.info("rate-limit resume cancelled pane=%d", pane.pane_id)
            return
        fresh = capture_pane_adaptive(pane.pane_id, cfg)
        if not fresh:
            log.warning("rate-limit resume: capture failed pane=%d", pane.pane_id)
            return
        cls = classify(fresh)
        # Once reset_epoch has passed we proceed regardless of whether the
        # limit phrase is still on screen — Claude Code TUI does NOT auto-
        # refresh after the limit lifts, so the stale "You've hit your limit"
        # text persists in the scrollback until the user (or daemon) sends
        # something. Only retry when Claude is genuinely still working
        # (active spinner / tool-output expand hint) or when we somehow
        # fired before the reset epoch.
        if cls == "working" or time.time() < reset_epoch:
            log.info("rate-limit resume: pane=%d not ready (class=%s, pre-reset=%s); retrying in 30s",
                     pane.pane_id, cls, time.time() < reset_epoch)
            new_epoch = time.time() + 30.0 - buffer_s
            state.rate_limit_resume_at = new_epoch
            state.rate_limit_task = asyncio.create_task(
                _resume_after_rate_limit(pane, state, new_epoch, cfg, dry_run)
            )
            return
        if cls != "input":
            log.info("rate-limit resume: pane=%d now class=%s; aborting send",
                     pane.pane_id, cls)
            return
        resume_text = str(cfg.get("rate_limit_resume_text", "繼續"))
        # Claude Code TUI's queued-draft auto-flush is unreliable when the
        # underlying API connection has dropped (the rate-limit error path
        # often leaves the TUI in a "disconnected" state where queued
        # `｜<text>` drafts never auto-submit even after the limit lifts).
        # Always type the resume text + Enter to guarantee a visible submit —
        # accept the small risk of a duplicate if Claude eventually does
        # auto-flush a matching queued draft. Users who don't want this
        # behaviour can clear their queue or set rate_limit_resume_text="".
        queued = _find_queued_drafts(fresh)
        if queued:
            log.info("rate-limit resume: pane=%d existing queued drafts %r — typing anyway",
                     pane.pane_id, queued)
        if not resume_text:
            log.info("rate-limit resume: pane=%d resume_text empty; aborting", pane.pane_id)
            return
        if dry_run:
            log.info("rate-limit resume DRY-RUN pane=%d would type %r", pane.pane_id, resume_text)
            outcome = f"dry-run:text:{resume_text}"
        else:
            try:
                outcome = await apply_action(
                    pane, "text", resume_text,
                    baseline=fresh,
                    scrollback=int(cfg["capture_scrollback_lines"]),
                )
            except Exception as e:
                log.exception("rate-limit resume apply_action failed pane=%d", pane.pane_id)
                outcome = f"apply-error:{e}"
        log.info("rate-limit resume sent pane=%d outcome=%s", pane.pane_id, outcome)
        try:
            audit(
                pane, fresh,
                {"action": "text", "value": resume_text,
                 "source": "rate-limit-resume"},
                f"rate-limit-resume:{outcome}", cfg,
            )
        except Exception:
            log.exception("audit write failed (rate-limit resume)")
    finally:
        state.rate_limit_resume_at = 0.0
        state.rate_limit_task = None


def _schedule_rate_limit_resume(
    pane: Pane,
    state: PaneState,
    reset_epoch: float,
    cfg: dict[str, Any],
    dry_run: bool,
) -> None:
    """(Re-)schedule the resume task. If one is already pending for roughly
    the same epoch (±60s) keep it; otherwise cancel and create a fresh one
    so repeated detections of the same limit screen don't stack tasks."""
    existing = state.rate_limit_task
    if existing is not None and not existing.done():
        if abs(state.rate_limit_resume_at - reset_epoch) < 60.0:
            return
        existing.cancel()
    state.rate_limit_resume_at = reset_epoch
    state.rate_limit_task = asyncio.create_task(
        _resume_after_rate_limit(pane, state, reset_epoch, cfg, dry_run)
    )


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

            # Rate-limit short-circuit: highest priority, runs before any
            # codex round-trip. Deterministic signal — when Claude Code shows
            # "You've hit your limit · resets <time>", schedule a resume task
            # that types `rate_limit_resume_text` after the reset epoch passes.
            # No immediate keystroke is sent: the limit screen has no menu to
            # confirm, just an idle input box. Does not append to
            # responses_in_window — being throttled is a system response, not
            # a codex action, and must not count toward the killswitch.
            rl_epoch = detect_rate_limit(snapshot, cfg)
            if rl_epoch is not None:
                log.info("rate-limit detected pane=%d reset_epoch=%.0f wait=%.1fs",
                         pane.pane_id, rl_epoch, max(0.0, rl_epoch - time.time()))
                _schedule_rate_limit_resume(pane, state, rl_epoch, cfg, dry_run)
                state.cooldown_until = time.time() + float(cfg["per_pane_cooldown_seconds"])
                try:
                    audit(
                        pane, snapshot,
                        {"action": "skip", "value": "rate-limit-wait",
                         "source": "rate-limit-detect",
                         "resume_epoch": rl_epoch},
                        "rate-limited:scheduled", cfg,
                    )
                except Exception:
                    log.exception("audit write failed (rate-limit detect)")
                return

            # Best-effort: enrich the codex prompt with Claude Code session
            # metadata (title + initial/current user request) when we have a
            # transcript_path from a recent Stop-hook payload. None on
            # polling-only panes — codex falls back to screen-only reasoning.
            session_meta = extract_session_meta(state.transcript_path)

            # Pre-codex short-circuits: cache hit, then heuristic skip predictor.
            # Mix current_request into the cache key so the same visual prompt
            # under different user intents does not share a skip decision.
            clean = _clean_screen(snapshot)
            cache_context = (session_meta or {}).get("current_request", "")
            shash = _screen_hash(clean, cache_context)
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

            codex_prompt = build_prompt(snapshot, cfg, session_meta=session_meta)
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
    cls = classify(screen)
    if cls == "working":
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
    transcript_path: str | None = None,
) -> None:
    """Stop-hook arrived. Find pane, capture once, evaluate with stable-check bypassed.

    transcript_path is the Claude Code session JSONL forwarded by the hook
    script from its stdin payload. Stored on PaneState so subsequent poll
    ticks for the same pane can also enrich the codex prompt (best-effort —
    may go stale if user switches sessions; safe because screen-truth rule
    overrides any context mismatch).
    """
    log.info("hook recv pane=%d", pane_id)
    panes = discover_panes(os.getpid())
    pane = next((p for p in panes if p.pane_id == pane_id), None)
    if pane is None:
        log.info("hook event for unknown pane %d; ignored", pane_id)
        return
    state = states.setdefault(pane.pane_id, PaneState())
    if transcript_path:
        state.transcript_path = transcript_path
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

            transcript_path = str(msg.get("transcript_path") or "").strip() or None

            try:
                state_data = read_toggle_state(cfg)
                entry = state_data.get("panes", {}).get(str(pane_id))
                if entry and entry.get("enabled"):
                    await handle_milestone_event(pane_id, states, cfg, dry_run)
                else:
                    await handle_hook_event(
                        pane_id, states, cfg, dry_run,
                        transcript_path=transcript_path,
                    )
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
    acquire_singleton_lock()
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


async def run_once(cfg: dict[str, Any], dry_run: bool = False) -> int:
    """Single-tick test mode: discover, classify, print — then synchronously
    drive `handle_pane` for every `input`/`menu` pane so codex is actually
    invoked (with full audit log + cache + predictor short-circuits).

    Bypasses `should_trigger` killswitch/cooldown because fresh `PaneState`
    instances are used per invocation — there is no per-pane history to gate
    against. Honors `--dry-run` (no send-text)."""
    panes = discover_panes(os.getpid())
    if not panes:
        print("no wezterm panes found (is WezTerm GUI running?)")
        return 0
    triggerable: list[tuple[Pane, str, str]] = []
    for p in panes:
        screen = capture_pane_adaptive(p.pane_id, cfg)
        cls = classify(screen)
        print(f"pane={p.pane_id:<4} window={p.window_id} tab={p.tab_id} "
              f"class={cls:<8} title={p.title!r}")
        if cls in ("input", "menu") and screen:
            triggerable.append((p, cls, screen))
    if not triggerable:
        print("no input/menu panes — nothing to send to codex")
        return 0
    print(f"triggering codex for {len(triggerable)} pane(s) "
          f"(dry_run={dry_run}) ...")
    states: list[PaneState] = []
    for pane, cls, screen in triggerable:
        state = PaneState()
        state.last_classification = cls
        state.in_flight = True
        await handle_pane(pane, state, screen, cfg, dry_run)
        states.append(state)
    # `handle_pane` may have scheduled a background `rate_limit_task` that
    # fires `rate_limit_resume_buffer_seconds` after the reset epoch. Without
    # explicitly awaiting them, `asyncio.run()` tears down the loop and
    # cancels every pending task before the resume actually executes —
    # producing the "scheduled / cancelled in the same second" log entries
    # users see when smoke-testing rate-limit handling with `--once`. Await
    # any live resume tasks so the single-tick run actually exercises the
    # resume path.
    pending = [s.rate_limit_task for s in states
               if s.rate_limit_task is not None and not s.rate_limit_task.done()]
    if pending:
        print(f"awaiting {len(pending)} rate-limit resume task(s) ...")
        await asyncio.gather(*pending, return_exceptions=True)
    return 0


# ---------- CLI entrypoint -----------------------------------------------------

def cli_entry() -> None:
    ap = argparse.ArgumentParser(description="Auto-respond to Claude prompts via Codex CLI.")
    ap.add_argument("--once", action="store_true", help="single tick: classify + invoke codex for every input/menu pane, then exit (combine with --dry-run to skip send-text)")
    ap.add_argument("--dry-run", action="store_true", help="full loop but skip send-keys")
    args = ap.parse_args()
    cfg = load_config()
    if args.once:
        cfg["log_enabled"] = True
    setup_logging(cfg)
    if args.once:
        sys.exit(asyncio.run(run_once(cfg, dry_run=args.dry_run)))
    try:
        asyncio.run(run_daemon(cfg, dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli_entry()
