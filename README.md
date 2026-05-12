# watcher

監聽 WSL2 tmux 裡所有跑 Claude Code 的 pane，偵測到「等使用者輸入」時把畫面餵給 Codex CLI 決定怎麼回，再 `tmux send-keys` 送答案回去。

```
                      ┌─► Claude Code Stop hook ─► unix socket ─► watcher ─┐
tmux pane (claude) ───┤                                                    ├─► codex exec ─► tmux send-keys
                      └─► 每 3 分鐘輪詢 (fallback) ────────────────────────┘
```

主觸發是 Stop hook（即時、零延遲）；輪詢只是 fallback。專案以 **Claude Code plugin** 形式發行：marketplace 安裝負責 hook 自動註冊；daemon 則從 clone 出來的目錄跑（plugin 不會代啟動）。

---

## 環境需求

- WSL2（Linux 端）+ `tmux` ≥ 3.x
- `uv`（Python 套件/環境管理器）
- Node.js + `@openai/codex` + 已 `codex login`（ChatGPT 訂閱授權）

codex CLI 必須裝在 **WSL2 端**，不要從 Windows `cmd.exe` 借用。

---

## 安裝

**1. 裝 plugin（hook + slash commands）**

在 Claude Code 內：

```
/plugin marketplace add j7-dev/watcher
/plugin install watcher@watcher
```

或 CLI：

```bash
claude plugin marketplace add j7-dev/watcher
claude plugin install watcher@watcher
```

**2. clone repo 跑 daemon**

```bash
git clone https://github.com/j7-dev/watcher ~/DEV/watcher
cd ~/DEV/watcher
uv sync
uv run watcher.py
```

> 為什麼分兩步：marketplace 把 plugin 放在 `~/.claude/plugins/cache/.../<sha>/`，路徑會跟著版本 SHA 變動，不適合做 daemon 啟動目錄。Hook 走 `${CLAUDE_PLUGIN_ROOT}` 自動解析沒問題；daemon 從固定 clone 路徑跑比較穩。

建議常駐方式：

```bash
tmux new -s watcher 'cd ~/DEV/watcher && uv run watcher.py'
# 或背景：cd ~/DEV/watcher && nohup uv run watcher.py >/dev/null 2>&1 &
```

---

## 更新

```
/plugin update watcher@watcher
```

或 CLI：

```bash
claude plugin marketplace update watcher   # 拉最新 marketplace 定義
claude plugin update watcher@watcher       # 升級已安裝的 plugin
```

Daemon 端：

```bash
cd ~/DEV/watcher && git pull && uv sync
pkill -f watcher.py && uv run watcher.py   # 或在 tmux pane 內 Ctrl-C 重啟
```

---

## 移除

```
/plugin uninstall watcher@watcher
/plugin marketplace remove watcher
```

或 CLI：

```bash
claude plugin uninstall watcher@watcher
claude plugin marketplace remove watcher
```

Daemon：`pkill -f watcher.py`。clone 目錄可選擇刪除。

---

## 主要用法

watcher 是常駐 daemon。三種啟動模式：

| 指令 | 用途 |
|------|------|
| `uv run watcher.py --once` | 跑一輪偵測就結束，不呼叫 codex、不 send-keys。第一次驗證 pane 偵測 |
| `uv run watcher.py --dry-run` | 完整迴圈呼叫 codex，但**不** send-keys。建議搭配 `WATCHER_LOG_ENABLED=true` |
| `uv run watcher.py` | 正式模式 |

watcher 透過 `pane_current_command == "claude"` 過濾，不會把自己誤判。停止：前景 `Ctrl-C`，背景 `pkill -f watcher.py`。已處理 SIGINT/SIGTERM。

驗證 hook：在另一 pane 跑 Claude Code 回一個 turn → watcher stderr 幾秒內應印 `class=input src=hook`。Socket 在 `${XDG_RUNTIME_DIR:-/tmp}/watcher-$USER.sock`。

---

## 設定

**優先序**：`WATCHER_<KEY>` 環境變數 > `config.toml` > 內建預設值。設定檔位置：clone 目錄下的 `config.toml`（可省略；缺檔走預設）。改 `config.toml` 後**必須重啟 watcher**。

### 可調項目

| 鍵 | 預設 | 說明 |
|----|------|------|
| **觸發** | | |
| `poll_interval_seconds` | `180` | fallback 輪詢間隔（秒） |
| `stable_count_required` | `2` | 輪詢路徑：連續幾次擷取畫面相同才算靜止。hook 路徑跳過此檢查 |
| `per_pane_cooldown_seconds` | `15` | 同 pane 觸發後幾秒內不再觸發 |
| `max_responses_per_window` | `5` | killswitch：時間窗內最多回應次數 |
| `response_window_minutes` | `5` | killswitch 時間窗 |
| **Hook 通道** | | |
| `socket_enabled` | `true` | 開啟 unix socket 接 Stop hook |
| `socket_path` | `""` | 空 = `$XDG_RUNTIME_DIR/watcher-$USER.sock`（fallback `/tmp/...`） |
| **Codex** | | |
| `codex_binary` | `"codex"` | codex 執行檔 |
| `codex_timeout_seconds` | `90` | 單次 `codex exec` 最長等待 |
| **畫面擷取** | | |
| `capture_scrollback_lines` | `200` | 抓畫面含多少行 scrollback |
| `hr_min_length` | `50` | 水平線最小 `─` 字元數 |
| **日誌** | | |
| `log_enabled` | `false` | 整體 log 開關。預設關（快照可能含 token） |
| `log_max_bytes` | `10_000_000` | `watcher.log` 大小上限（byte） |
| `log_backups` | `3` | rotate 後保留份數 |
| `log_retention_days` | `0` | `>0` 時每小時清 `logs/triggers/` 內 mtime 超過 N 天的檔 |

### 常用 override

```bash
WATCHER_LOG_ENABLED=true WATCHER_POLL_INTERVAL_SECONDS=60 uv run watcher.py
WATCHER_SOCKET_ENABLED=false uv run watcher.py   # 純輪詢、停 hook 通道
```

---

## 工作原理

**路徑 A：Stop hook（主）**
1. Claude Code turn 結束 → 觸發 Stop hook
2. `claude-stop-notify.py` 讀 `$TMUX_PANE`，連 unix socket、寫 pane id
3. watcher 收到 → `tmux capture-pane` → 分類 → codex → send-keys
4. **跳過** `stable_count_required`；其他 gate 照常

**路徑 B：輪詢（fallback）**
1. 每 `poll_interval_seconds` 跑 `tmux list-panes -a`，過濾 `pane_current_command == "claude"`
2. 連續 `stable_count_required` 次擷取相同才算靜止

**分類** (`classify()`)：
- `working`：title 是 Braille spinner（U+2800–U+28FF），或畫面尾段含 `esc to interrupt` / `(ctrl+o to expand)` → 跳過
- `menu`：尾段有 `❯ 1.`、`❯ 2.` 選單
- `input`：底部 `─...─` + `❯ ` 空輸入 + `─...─` 輸入框
- `other`：以上皆非 → 跳過

只有 `input` 與 `menu` 送 codex。

**Codex 回傳** (`response_schema.json` 強制)：

```json
{ "action": "text" | "key" | "enter" | "skip", "value": "..." }
```

- `text` → `tmux send-keys -l <text>` + Enter
- `key` → 送數字鍵 + Enter
- `enter` → 只送 Enter
- `skip` → 不動，記錄

送出前**再抓一次**畫面比對；若已被人動過就 abort。

> Killswitch 跳閘後該 pane **永久**停用到 daemon 重啟。防 codex/claude 互相鏈式對話爆走。

---

## 日誌與審計

預設 `log_enabled = false`——只輸出 stderr，不落地（畫面快照可能含 token / API key）。開啟後：

```
logs/                              # 0700，.gitignore 已加
├── watcher.log                    # 主 log（rotate：10MB × 3 份）
└── triggers/
    ├── 1715500000-3.json          # 每次觸發：快照 + codex 回覆 + 動作
    └── codex-out-*.txt
```

---

## 疑難排解

| 症狀 | 修法 |
|------|------|
| log 沒出現 `src=hook` | `claude --debug` 看 hook 是否註冊；確認 `/plugin list` 有 watcher |
| `hook event for unknown pane %N` | 重開 Claude Code |
| hook timeout | 確認 daemon 還活著、socket 檔還在 |
| `codex: command not found` | `which codex` + `codex login status`，確認裝在 WSL2 端 |
| watcher 偵測不到 pane | `uv run watcher.py --once` 看分類；`pane_current_command` 必須是 `claude` |
| killswitch 一直跳 | 調高 `per_pane_cooldown_seconds` 或降 `max_responses_per_window`，跳了**必須重啟 daemon** |
| codex 一直回 `skip` | 開 log 看 `logs/triggers/*.json`，常因提示太保守 |

---

## Plugin 結構

```
watcher/
├── .claude-plugin/
│   ├── plugin.json          # plugin manifest
│   └── marketplace.json     # marketplace 定義（單 plugin）
├── hooks/
│   ├── hooks.json           # 自動註冊 Stop hook（用 ${CLAUDE_PLUGIN_ROOT}）
│   └── claude-stop-notify.py
├── commands/                # /watcher:* slash commands
├── scripts/                 # release.sh 等維運腳本
├── watcher.py               # daemon 本體（plugin 不啟動，要自己跑）
├── config.toml
└── response_schema.json
```

---

## 已知限制

- 沒有 systemd unit / 開機自動啟動
- Codex 看不到 project 脈絡（只看單一 pane 畫面）
- Persona 內嵌在 `PROMPT_TEMPLATE`，不支援多 persona 切換
