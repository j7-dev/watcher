# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概要

`watcher` 是一個 Python 3.12+ 常駐 daemon（標準函式庫、`uv` 管理），監看 **Windows 上 WezTerm** 的每一個 pane；偵測到「閒置 prompt」時擷取畫面，丟給 Codex CLI 決定回什麼，再 `wezterm cli send-text` 送回去。同時以 **Claude Code plugin** 形式發行（marketplace 自動註冊 Stop hook；daemon 仍需從 clone 目錄手動跑）。

兩條觸發路徑並存：

- **Stop hook（主路徑、即時）**：Claude Code turn 結束 → `hooks/claude-stop-notify.py` 把 `$WEZTERM_PANE` 透過 **TCP loopback** 推給 daemon → 直接評估。
- **輪詢（fallback）**：`poll_interval_seconds`（預設 60s）跑一次 `wezterm cli list --format json`；每次擷取分類後即評估觸發條件（無連續靜止判斷）。**不做 PID pre-filter**——所有 pane 都 capture，靠 `classify()` 視覺指紋（NBSP `❯` + HR 線 + `esc to interrupt` footer）過濾。

## 常用指令

```powershell
uv sync                          # 安裝依賴（首次或更新後）
uv run watcher.py                # 正式跑 daemon
uv run watcher.py --once         # 單次偵測 + 分類列印，不呼叫 codex
uv run watcher.py --dry-run      # 完整迴圈呼叫 codex，但不 send-text
uv run watcher                   # 等同於上面（pyproject 已定義 entry point）

# daemon 控制（會起一個 WezTerm window/pane 跑 watcher）
pwsh scripts\watcher-daemon.ps1 status
pwsh scripts\watcher-daemon.ps1 start
pwsh scripts\watcher-daemon.ps1 stop

# attach 看 live log（daemon 跑在獨立 wezterm pane 內）
wezterm cli activate-pane --pane-id <id>   # status 會印對應 id

# Stop hook 安裝（寫入 ~/.claude/settings.json）
python scripts\install-hook.py            # install
python scripts\install-hook.py --status
python scripts\install-hook.py --uninstall

# 常用 env override（PowerShell 語法）
$env:WATCHER_LOG_ENABLED = "true"; $env:WATCHER_POLL_INTERVAL_SECONDS = "60"; uv run watcher.py
$env:WATCHER_SOCKET_ENABLED = "false"; uv run watcher.py    # 停 hook 通道，純輪詢
$env:WATCHER_SOCKET_PORT = "47900"; uv run watcher.py       # 改用其他 port
```

任何 Python 操作**一律使用 `uv`，不要退回 `python -m venv` + `pip install` 流程**。專案沒有 test suite、沒有 linter 設定。

## 高層架構

整支 daemon 集中在 `watcher.py`。理解程式需要先把這幾個概念串起來：

### 設定解析優先序

`WATCHER_<KEY>` 環境變數 > `config.toml` > `watcher.py:DEFAULTS`。`load_config()` 用 `_coerce` 依 `DEFAULTS` 的型別把 env var 字串轉成對應型別（list 走 `,` 分隔）。**改 `config.toml` 必須重啟 daemon 才生效。**

### Pane 偵測與分類（`classify()`）

`wezterm cli list --format json` 列出全部 pane（**不像 tmux 有 `pane_current_command` 可 pre-filter**——WezTerm JSON 只給 `pane_id` / `window_id` / `tab_id` / `workspace` / `title` / `cwd`，沒有可信賴的「是否在跑 claude」欄位）。對每個 pane 跑 `wezterm cli get-text` 抓畫面，靠 `classify()` 視覺指紋判定三類，只有 `input` 與 `menu` 會送 codex：

- `working`：title 首字是 Braille spinner（U+2800–U+28FF），**或**畫面**最後 5 行**含 `esc to interrupt` / `(ctrl+o to expand)`。只看尾段是刻意的——`(ctrl+o to expand)` 在折疊的 tool-output（`+N lines (ctrl+o to expand)`）也會出現，掃到 scrollback 就會誤判 working。
- `menu`：尾段有 `❯ 1.` / `❯ 2.` 編號選單（`MENU_CHOICE_RE`）。
- `input`：尾段呈現「水平線 + `❯ ` 空輸入 + 水平線」的輸入框。
  - 水平線判定用 `line.count("─") >= 50`（`is_hr_line`）而非整行全 `─` 的 regex——頂部水平線會嵌入 session 標籤 `─── claude-codex-auto-responder ──`。
  - 空 prompt 行尾是 **NBSP（U+00A0）**而非半形空格。用 `is_empty_prompt_line()` 比對 `s.strip() == "❯"`，**不要**用 regex 配空格。該行也可能帶 dim placeholder `❯ Try "..."`，函式也認這格式。

> WezTerm 的 alt-screen 限制：`wezterm cli get-text --start-line -N` 對 Claude Code TUI 通常只能拿到 viewport（~40-50 行），拿不到 alt-screen 之上的歷史。`capture_scrollback_lines` 預設 100 已足以涵蓋 classify 最大 lookback（input 25 行、menu 40 行）。

### Pane ID 是 int

WezTerm pane id 是**整數**（例如 `4`、`13`），不是 tmux 的 `%18`。`Pane.pane_id: int`，audit log JSON 的 `pane_id` 欄位也是 int（**對讀 audit log 的下游工具是 breaking change**）。`config.toml` 的 `log_pane_ids` 接受 `[18, 19]` 或 `["18", "19"]` 或歷史 `["%18", "%19"]`（`%` 前綴會被剝掉）。

### 觸發 gate 流程（`should_trigger` → `evaluate_pane` → `handle_pane`）

通過分類後依序檢查：`disabled` / `in_flight` → 冷卻 `cooldown_until` → 是否屬於 `input`/`menu` → killswitch 時間窗（`response_window_minutes` 內超過 `max_responses_per_window` 次永久停用該 pane，**只能重啟 daemon 才能恢復**）。

`handle_pane` 內**呼叫 codex 前**還有兩道便宜短路（省 token / 省 codex round-trip）：

1. **決策快取**：key 為 `sha256(_clean_screen(snapshot))`。codex 回 `skip` 或預測器回 `skip` 都會塞進去。TTL 由 `skip_decision_cache_ttl_seconds` 控制（預設 300s）。cache hit → outcome `cached-skip`，**不計入 killswitch**。
2. **`predict_skip` 啟發式**：只對 `classification == "input"` 觸發（`menu` 一定有編號選項，沒必要預測）。掃描清理後畫面最後 `skip_predictor_lookback_lines` 行非空行，若**沒有**任何 `DEFAULT_QUESTION_MARKERS` 字串（`?`、`？`、`do you`、`continue`、`confirm`、`是否`、`請問` …）也沒有 `\d+[.)]` 開頭的編號行 → 視為閒置 prompt、outcome `predicted-skip`。同樣**不計入 killswitch**。

> 經 codex 真實呼叫的回應才會 append 到 `responses_in_window`。

### Codex 呼叫（`call_codex`，與 codex-cli 0.130.0 驗證過）

```
codex exec --ephemeral --skip-git-repo-check --sandbox read-only \
  -C <WATCHER_DIR_FORWARD_SLASH> \
  --output-schema response_schema.json \
  --output-last-message <out_file> \
  "<full_prompt>"
```

- `--ephemeral` 必加，避免 openai/codex#11435 的平行 session-restore bug。
- `--ask-for-approval` 在 `codex exec` 已被移除——非互動模式由 `--sandbox` 反推 approval policy；`read-only` 等同無 approval prompt。
- **`-C` Windows 路徑**：`str(WATCHER_DIR).replace("\\", "/")` 轉 forward slash 再傳——避開反斜線在 codex CLI 內部被當 escape 的可能性。實機 spike 顯示 codex 啟動較慢（10-30s），這是 codex 本身行為，與路徑無關。
- subprocess gotcha：stdout 設 `DEVNULL`、stderr 走 PIPE，避免 deadlock；POSIX 用 `start_new_session=True`、Windows 用 `creationflags=CREATE_NEW_PROCESS_GROUP`；timeout 後 `terminate` 再 5s `kill`。
- 回傳 JSON 由 `response_schema.json`（strict structured output）強制。注意 strict 模式**所有欄位都必須在 `required`**，optional 欄位只能宣告成 `["string", "null"]` 並回 `null`，**不可直接省略 key**。

### Action 套用（`apply_action`）

在 send-text 前**再抓一次畫面**和 baseline 比對，不同就回 `aborted-pane-changed`，防止 codex 思考期間人為操作被覆蓋。`action`：

- `text` → 兩次 `wezterm cli send-text --no-paste --pane-id N` call：第一次送 `<value>`（無 CR）、`asyncio.sleep(0.15)`、第二次送 `\r`。**不要把 text + `\r` 合在同一 payload**——Claude Code 的輸入處理會把整批當 paste-like 處理，trailing `\r` 變成「輸入內換行」而非 submit。兩 call 都帶 `--no-paste`、中間 small delay 讓 TUI 把第二次當乾淨的 Enter keystroke。
- `key`  → 單次 call，payload `<digit>\r`（`value` 必須是長度 1 的數字）。短 payload 不會觸發 paste-like 啟發。
- `enter` → 單次 call，payload `\r`
- `skip` → 不動、寫 audit

**重要**：`--no-paste` 是強制的——預設 bracketed-paste 模式會被 Claude Code TUI 當作貼上資料而非按鍵，可能不會自動送出。長字串 + `\r` 的單次 call 即使加 `--no-paste` 仍會被 TUI 當 paste burst → 拆兩次 call 才能穩定 submit（已實測長中文 reply 失敗、拆 call 後正常）。

### Stop hook 通道（TCP loopback）

WezTerm/Windows 沒有 unix domain socket。daemon 在 `127.0.0.1:<socket_port>`（預設 47823）開 TCP server，handler 額外驗 peer 是 loopback。port 被占用時自動嘗試 `+1..+10`。binding 成功後寫 `~/.watcher/socket-info.json` 紀錄 host/port/pid/started_at，給 hook script 找 daemon 用：

```
WATCHER_SOCKET_HOST  > WATCHER_SOCKET_PORT  > socket-info.json  > 預設 127.0.0.1:47823
```

可選：`socket_token`（共享祕密）開啟後 payload 必須帶相同 token，多 user 環境防偽造。

### Hook script 取 pane_id

Phase 1 spike R3 驗證 **Claude Code 會把 `WEZTERM_PANE` 環境變數傳入 Stop hook child process**（透過 Python `os.environ`），所以 `hooks/claude-stop-notify.py` 直接讀 `os.environ["WEZTERM_PANE"]` 即可。Bonus：Claude Code 也透過 stdin 傳 JSON payload（含 `session_id` / `transcript_path` / `cwd` / `hook_event_name` / `last_assistant_message`），目前未使用但保留為未來 fallback。

### Audit log

`log_enabled = true` 才會寫：`logs/watcher.log`（RotatingFileHandler，10MB × 3）+ `logs/triggers/<ts>-<pane_id>.json`（含完整 snapshot）。`logs/` 目錄是 `0700` 並列入 `.gitignore`，因為**畫面快照可能含 token / API key**。`log_pane_ids = [18, ...]` 可限定哪些 pane 寫 triggers/codex-out（主 `watcher.log` 仍全寫）。

## Plugin 結構與 release 流程

- `.claude-plugin/plugin.json` 與 `marketplace.json` 是 plugin 與 marketplace 描述（單 plugin 的 marketplace 倉）。
- `hooks/hooks.json` 自動把 `claude-stop-notify.py` 註冊為 Stop hook，路徑用 `${CLAUDE_PLUGIN_ROOT}` 解析（marketplace 安裝會被放在 `~/.claude/plugins/cache/.../<sha>/`）。Hook command 是 `python "${CLAUDE_PLUGIN_ROOT}/hooks/claude-stop-notify.py"`——Windows 無 shebang 機制，**必須**顯式呼叫 `python`。
- `commands/*.md` 是 `/watcher:*` slash command 包裝；它們**都應該透過 `${CLAUDE_PLUGIN_ROOT}/scripts/...` 呼叫腳本**，不要自己 Edit/Write `settings.json`，會繞過 `install-hook.py` 的 `.bak` + 原子寫入。
- Daemon 不由 plugin 啟動——使用者要從固定 clone 路徑跑 `uv run watcher.py`，因為 plugin cache 路徑會隨版本 SHA 變動。`watcher-daemon.ps1` 用 `$env:WATCHER_REPO` 或腳本上層目錄決定 repo 位置。
- Daemon 本身**跑在 WezTerm 的獨立 window/workspace（`--workspace watcher`）內**——可 `wezterm cli activate-pane --pane-id <id>` attach 看 live log。state file `~/.watcher/daemon-state.json` 存 `pane_id + wezterm_pid + StartTime`；status 用「wezterm-gui pid + StartTime 比對 + pane id 還在 list」三條件確認 daemon 活著（防 PID 重用後誤判）。

**Release 自動化（推 master 即發版）**：pre-push hook 追蹤在 `scripts/git-hooks/pre-push`，clone 後必須先跑一次 `git config core.hooksPath scripts/git-hooks` 才會啟用（`.git/hooks/` 不在 git 追蹤範圍內，所以走 `core.hooksPath` 才能版本管理）。**pre-push 仍是 bash 殼**（git-for-windows 自帶 bash + 內部呼叫 `python`），推 master 會自動跑 `scripts/release.sh`：

1. patch-bump `.claude-plugin/plugin.json` 的 `version`
2. commit + 打 `vX.Y.Z` tag
3. 用 `RELEASING=1` env var 把 master 與 tag 推上去（防 hook 遞迴）
4. 回 exit 1 取消原本的 push（release.sh 已自己推完，原 push 是多餘的）——**這是預期行為，不要修**

意思是：在 master 上 `git push` 會自動 bump version；不要手改 `plugin.json` 的 `version` 欄位，也不要手打 tag。如果是 feature branch，pre-push 直接 short-circuit、行為跟一般 push 一樣。

## 編輯時要注意的點

- **不要在 `is_empty_prompt_line` / `is_hr_line` 改成嚴格 regex**——上面說明的 NBSP 與嵌入式 session label 都會破壞 strict match。
- **不要把 `(ctrl+o to expand)` 的偵測範圍擴大到整個 scrollback**，會誤把 collapsed tool-output 判成 working。
- 加新分類前先確認 `should_trigger` 的 `classification not in ("input", "menu")` 過濾；想讓新分類觸發 codex 必須一起改。
- 改 `PROMPT_TEMPLATE` 時保持「PREFER MAKING A DECISION OVER SKIPPING」的調性；之前的保守版會在明明可答的畫面回太多 `skip`。
- 加新 question marker：用 `config.toml` 的 `skip_predictor_extra_markers`（小寫 substring 比對），不要直接動 `DEFAULT_QUESTION_MARKERS`。
- 新增 config key：必須同時加進 `DEFAULTS` dict（type 決定 env var coerce 行為），TOML 與 env var 才會被解析。
- **不要把 `pane_id` 改回字串**——它是 int，貫穿 audit log、state file key、wezterm cli 參數。`milestone-toggle.json` 內 key 雖然是 str（JSON 強制），但讀寫時都用 `str(pane_id)` 包裝。
- **`wezterm cli` 任何呼叫都要 wrap try/except**（`CalledProcessError` / `TimeoutExpired` / `FileNotFoundError`），失敗時 warn log + 空回傳值，**不允許 daemon 因此 crash**。新增 wezterm 呼叫處請照樣處理。
- **WezTerm GUI 未啟動時** `wezterm cli list` 會失敗——daemon 應 graceful 空集合下一輪重試，**不退出**。
- **`apply_action` send-text 必加 `--no-paste`**。`key` / `enter` 兩個 action 把 `\r` 嵌在 payload；`text` 必須拆兩次 call（text → sleep 0.15s → `\r`），否則長字串會被 Claude Code TUI 當 paste 處理 trailing CR 不 submit。
- Codex CLI 旗標若報錯，先看 `https://developers.openai.com/codex/cli/reference`——CLI 改版頻繁（例如 `--ask-for-approval` 已被砍）。
- Windows 平台 gotcha：`loop.add_signal_handler` 在 ProactorEventLoop 會 raise `NotImplementedError`，已用 try/except 包裹；`start_new_session` 是 POSIX-only，Windows 走 `creationflags=CREATE_NEW_PROCESS_GROUP` 路徑。
