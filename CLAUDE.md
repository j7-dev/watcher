# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概要

`watcher` 是一個 Python 3.12+ 常駐 daemon（標準函式庫、`uv` 管理），監看 WSL2 tmux 中 `pane_current_command == "claude"` 的每一個 pane；偵測到「閒置 prompt」時擷取畫面，丟給 Codex CLI 決定回什麼，再 `tmux send-keys` 送回去。同時以 **Claude Code plugin** 形式發行（marketplace 自動註冊 Stop hook；daemon 仍需從 clone 目錄手動跑）。

兩條觸發路徑並存：

- **Stop hook（主路徑、即時）**：Claude Code turn 結束 → `hooks/claude-stop-notify.py` 把 `$TMUX_PANE` 透過 unix socket 推給 daemon → 跳過 `stable_count_required` 檢查、直接評估。
- **輪詢（fallback）**：`poll_interval_seconds`（預設 180s）跑一次 `tmux list-panes -a`；要連續 `stable_count_required` 次擷取相同才算靜止。

## 常用指令

```bash
uv sync                          # 安裝依賴（首次或更新後）
uv run watcher.py                # 正式跑 daemon
uv run watcher.py --once         # 單次偵測 + 分類列印，不呼叫 codex
uv run watcher.py --dry-run      # 完整迴圈呼叫 codex，但不 send-keys
uv run watcher                   # 等同於上面（pyproject 已定義 entry point）

# daemon 控制（會起一個叫 watcher 的 detached tmux session）
bash scripts/watcher-daemon.sh status
bash scripts/watcher-daemon.sh start
bash scripts/watcher-daemon.sh stop

# Stop hook 安裝（寫入 ~/.claude/settings.json）
python3 scripts/install-hook.py            # install
python3 scripts/install-hook.py --status
python3 scripts/install-hook.py --uninstall

# 常用 env override
WATCHER_LOG_ENABLED=true WATCHER_POLL_INTERVAL_SECONDS=60 uv run watcher.py
WATCHER_SOCKET_ENABLED=false uv run watcher.py            # 停 hook 通道，純輪詢
```

任何 Python 操作**一律使用 `uv`，不要退回 `python -m venv` + `pip install` 流程**。專案沒有 test suite、沒有 linter 設定。

## 高層架構

整支 daemon 集中在 `watcher.py`（單檔約 920 行）。理解程式需要先把這幾個概念串起來：

### 設定解析優先序

`WATCHER_<KEY>` 環境變數 > `config.toml` > `watcher.py:DEFAULTS`。`load_config()` 用 `_coerce` 依 `DEFAULTS` 的型別把 env var 字串轉成對應型別（list 走 `,` 分隔）。**改 `config.toml` 必須重啟 daemon 才生效。**

### Pane 偵測與分類（`classify()`）

擷取 `capture_scrollback_lines` 行畫面後分四類，只有 `input` 與 `menu` 會送 codex：

- `working`：title 首字是 Braille spinner（U+2800–U+28FF），**或**畫面**最後 5 行**含 `esc to interrupt` / `(ctrl+o to expand)`。只看尾段是刻意的——`(ctrl+o to expand)` 在折疊的 tool-output（`+N lines (ctrl+o to expand)`）也會出現，掃到 scrollback 就會誤判 working。
- `drafting`：底部 ~15 行有以**全形 `｜`（U+FF5C）**開頭的行 → 使用者已 queued 草稿，不能介入。注意是全形 U+FF5C，不是半形 `|`（U+007C，statusline 也會用）。
- `menu`：尾段有 `❯ 1.` / `❯ 2.` 編號選單（`MENU_CHOICE_RE`）。
- `input`：尾段呈現「水平線 + `❯ ` 空輸入 + 水平線」的輸入框。
  - 水平線判定用 `line.count("─") >= 50`（`is_hr_line`）而非整行全 `─` 的 regex——頂部水平線會嵌入 session 標籤 `─── claude-codex-auto-responder ──`。
  - 空 prompt 行尾是 **NBSP（U+00A0）**而非半形空格。用 `is_empty_prompt_line()` 比對 `s.strip() == "❯"`，**不要**用 regex 配空格。該行也可能帶 dim placeholder `❯ Try "..."`，函式也認這格式。

### 觸發 gate 流程（`should_trigger` → `evaluate_pane` → `handle_pane`）

通過分類後依序檢查：`disabled` / `in_flight` → 冷卻 `cooldown_until` → 是否屬於 `input`/`menu` → （poll 路徑才檢查）`stable_count_required` 連續相同 → killswitch 時間窗（`response_window_minutes` 內超過 `max_responses_per_window` 次永久停用該 pane，**只能重啟 daemon 才能恢復**）。

`handle_pane` 內**呼叫 codex 前**還有兩道便宜短路（省 token / 省 codex round-trip）：

1. **決策快取**：key 為 `sha256(_clean_screen(snapshot))`。codex 回 `skip` 或預測器回 `skip` 都會塞進去。TTL 由 `skip_decision_cache_ttl_seconds` 控制（預設 300s）。cache hit → outcome `cached-skip`，**不計入 killswitch**。
2. **`predict_skip` 啟發式**：只對 `classification == "input"` 觸發（`menu` 一定有編號選項，沒必要預測）。掃描清理後畫面最後 `skip_predictor_lookback_lines` 行非空行，若**沒有**任何 `DEFAULT_QUESTION_MARKERS` 字串（`?`、`？`、`do you`、`continue`、`confirm`、`是否`、`請問` …）也沒有 `\d+[.)]` 開頭的編號行 → 視為閒置 prompt、outcome `predicted-skip`。同樣**不計入 killswitch**。

> 經 codex 真實呼叫的回應才會 append 到 `responses_in_window`。

### Codex 呼叫（`call_codex`，與 codex-cli 0.130.0 驗證過）

```
codex exec --ephemeral --skip-git-repo-check --sandbox read-only \
  -C <WATCHER_DIR> \
  --output-schema response_schema.json \
  --output-last-message <out_file> \
  "<full_prompt>"
```

- `--ephemeral` 必加，避免 openai/codex#11435 的平行 session-restore bug。
- `--ask-for-approval` 在 `codex exec` 已被移除——非互動模式由 `--sandbox` 反推 approval policy；`read-only` 等同無 approval prompt。
- subprocess gotcha：stdout 設 `DEVNULL`、stderr 走 PIPE，避免 deadlock；`start_new_session=True`；timeout 後 `terminate` 再 5s `kill`。
- 回傳 JSON 由 `response_schema.json`（strict structured output）強制。注意 strict 模式**所有欄位都必須在 `required`**，optional 欄位只能宣告成 `["string", "null"]` 並回 `null`，**不可直接省略 key**。

### Action 套用（`apply_action`）

在 send-keys 前**再抓一次畫面**和 baseline 比對，不同就回 `aborted-pane-changed`，防止 codex 思考期間人為操作被覆蓋。`action`：

- `text` → `send-keys -l <value>` + Enter
- `key`  → 單一數字 + Enter（`value` 必須是長度 1 的數字）
- `enter` → 只送 Enter
- `skip` → 不動、寫 audit

### Audit log

`log_enabled = true` 才會寫：`logs/watcher.log`（RotatingFileHandler，10MB × 3）+ `logs/triggers/<ts>-<pane>.json`（含完整 snapshot）。`logs/` 目錄是 `0700` 並列入 `.gitignore`，因為**畫面快照可能含 token / API key**。`log_pane_ids = ["%18", ...]` 可限定哪些 pane 寫 triggers/codex-out（主 `watcher.log` 仍全寫）。

## Plugin 結構與 release 流程

- `.claude-plugin/plugin.json` 與 `marketplace.json` 是 plugin 與 marketplace 描述（單 plugin 的 marketplace 倉）。
- `hooks/hooks.json` 自動把 `claude-stop-notify.py` 註冊為 Stop hook，路徑用 `${CLAUDE_PLUGIN_ROOT}` 解析（marketplace 安裝會被放在 `~/.claude/plugins/cache/.../<sha>/`）。
- `commands/*.md` 是 `/watcher:*` slash command 包裝；它們**都應該透過 `${CLAUDE_PLUGIN_ROOT}/scripts/...` 呼叫腳本**，不要自己 Edit/Write `settings.json`，會繞過 `install-hook.py` 的 `.bak` + 原子寫入。
- Daemon 不由 plugin 啟動——使用者要從固定 clone 路徑跑 `uv run watcher.py`，因為 plugin cache 路徑會隨版本 SHA 變動。`watcher-daemon.sh` 用 `WATCHER_REPO` env var 或腳本上層目錄決定 repo 位置。

**Release 自動化（推 master 即發版）**：pre-push hook 追蹤在 `scripts/git-hooks/pre-push`，clone 後必須先跑一次 `git config core.hooksPath scripts/git-hooks` 才會啟用（`.git/hooks/` 不在 git 追蹤範圍內，所以走 `core.hooksPath` 才能版本管理）。啟用後推 master 會自動跑 `scripts/release.sh`：

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
- Codex CLI 旗標若報錯，先看 `https://developers.openai.com/codex/cli/reference`——CLI 改版頻繁（例如 `--ask-for-approval` 已被砍）。
