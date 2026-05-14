# watcher

監聽 **Windows 上 WezTerm** 裡所有跑 Claude Code 的 pane，偵測到「等使用者輸入」時把畫面餵給 Codex CLI 決定怎麼回，再 `wezterm cli send-text` 送答案回去

```
                        ┌─► Claude Code Stop hook ─► TCP loopback ─► watcher ─┐
WezTerm pane (claude) ──┤                                                     ├─► codex exec ─► wezterm cli send-text
                        └─► 每 60s 輪詢 (fallback) ───────────────────────────┘
```

主觸發是 Stop hook（即時、零延遲）；輪詢只是 fallback。專案以 **Claude Code plugin** 形式發行：marketplace 安裝負責 hook 自動註冊；daemon 則從 clone 出來的目錄跑（plugin 不會代啟動）。

---

## 環境需求

- **Windows 10/11**（Windows-only — 已移除 WSL2 / tmux 支援）
- **WezTerm**（任意近期版本，需含 `wezterm cli`）
- **PowerShell 7+**（`pwsh`，用於 daemon 控制腳本）
- **uv**（Python 套件 / 環境管理器；自動建 .venv）
- **Python 3.12+**（uv 會處理）
- **Node.js + `@openai/codex` ≥ 0.130.0** + 已 `codex login`（ChatGPT 訂閱授權）
- **git for Windows**（含 bash，用於 pre-push release hook）

---

## 安裝

**1. 裝 plugin（hook + slash commands）**

在 Claude Code 內：

```
/plugin marketplace add j7-dev/watcher
/plugin install watcher@watcher
```

或 CLI：

```powershell
claude plugin marketplace add j7-dev/watcher
claude plugin install watcher@watcher
```

**2. clone repo 跑 daemon**

```powershell
git clone https://github.com/j7-dev/watcher $env:USERPROFILE\DEV\watcher
cd $env:USERPROFILE\DEV\watcher
uv sync
uv run watcher.py
```

> 為什麼分兩步：marketplace 把 plugin 放在 `~/.claude/plugins/cache/.../<sha>/`，路徑會跟著版本 SHA 變動，不適合做 daemon 啟動目錄。Hook 走 `${CLAUDE_PLUGIN_ROOT}` 自動解析沒問題；daemon 從固定 clone 路徑跑比較穩。

建議常駐方式（讓 daemon 跑在獨立 WezTerm window 內、可隨時 attach 看 log）：

```powershell
pwsh scripts\watcher-daemon.ps1 start
# attach 看 live log：
wezterm cli activate-pane --pane-id <印在 start 輸出的 id>
```

---

## 更新

```
/plugin update watcher@watcher
```

或 CLI：

```powershell
claude plugin marketplace update watcher
claude plugin update watcher@watcher
```

Daemon 端：

```powershell
cd $env:USERPROFILE\DEV\watcher
git pull
uv sync
pwsh scripts\watcher-daemon.ps1 stop
pwsh scripts\watcher-daemon.ps1 start
```

---

## 移除

```
/plugin uninstall watcher@watcher
/plugin marketplace remove watcher
```

Daemon：`pwsh scripts\watcher-daemon.ps1 stop`。clone 目錄可選擇刪除。

---

## 主要用法

watcher 是常駐 daemon。三種啟動模式：

| 指令 | 用途 |
|------|------|
| `uv run watcher.py --once` | 跑一輪偵測就結束，不呼叫 codex、不 send-text。第一次驗證 pane 偵測 |
| `uv run watcher.py --dry-run` | 完整迴圈呼叫 codex，但**不** send-text。建議搭配 `$env:WATCHER_LOG_ENABLED = "true"` |
| `uv run watcher.py` | 正式模式 |

watcher **不做 PID/process pre-filter**——`wezterm cli list` JSON 不含 process 欄位，所以 daemon 對所有 pane 都 capture，靠 `classify()` 視覺指紋（NBSP `❯` + HR 線 + Braille spinner）過濾出真正的 Claude pane。非 Claude pane 一律分類為 `other` 並跳過。

停止：前景 `Ctrl-C`，或透過 daemon 控制腳本 `pwsh scripts\watcher-daemon.ps1 stop`。

驗證 hook：在另一 pane 跑 Claude Code 回一個 turn → watcher stderr 幾秒內應印 `hook recv pane=N`。Daemon 監聽 port 寫在 `~/.watcher/socket-info.json`。

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
| **Hook 通道 (TCP loopback)** | | |
| `socket_enabled` | `true` | 開啟 TCP server 接 Stop hook |
| `socket_host` | `"127.0.0.1"` | 通常不需要改；非 loopback 會被 handler 拒絕 |
| `socket_port` | `47823` | port 被占用會自動嘗試 +1..+10，實際綁定值寫到 `~/.watcher/socket-info.json` |
| `socket_token` | `""` | 非空時 hook payload 必須帶相同 token（多 user 環境用） |
| **Codex** | | |
| `codex_binary` | `"codex"` | codex 執行檔 |
| `codex_timeout_seconds` | `90` | 單次 `codex exec` 最長等待 |
| **畫面擷取** | | |
| `capture_scrollback_lines` | `100` | 抓畫面含多少行 scrollback。WezTerm 對 alt-screen 通常只能拿到 viewport（~40-50 行）；100 已足以涵蓋 classify 最大 lookback |
| `hr_min_length` | `50` | 水平線最小 `─` 字元數 |
| **日誌** | | |
| `log_enabled` | `false` | 整體 log 開關。預設關（快照可能含 token） |
| `log_max_bytes` | `10_000_000` | `watcher.log` 大小上限（byte） |
| `log_backups` | `3` | rotate 後保留份數 |
| `log_retention_days` | `0` | `>0` 時每小時清 `logs/triggers/` 內 mtime 超過 N 天的檔 |
| `log_pane_ids` | `[]` | 只把 `logs/triggers/*.json` 與 `codex-out-*.txt` 限定在指定 pane（主 `watcher.log` 仍全量）。元素接受 `18` / `"18"` / `"%18"` |

### 常用 override（PowerShell）

```powershell
$env:WATCHER_LOG_ENABLED = "true"; $env:WATCHER_POLL_INTERVAL_SECONDS = "60"; uv run watcher.py
$env:WATCHER_SOCKET_ENABLED = "false"; uv run watcher.py        # 純輪詢、停 hook 通道
$env:WATCHER_LOG_PANE_IDS = "18,19"; uv run watcher.py          # 只記指定 pane 的 audit
$env:WATCHER_SOCKET_PORT = "47900"; uv run watcher.py           # 自訂 port
```

---

## 工作原理

**路徑 A：Stop hook（主）**
1. Claude Code turn 結束 → 觸發 Stop hook
2. `claude-stop-notify.py` 讀 `$WEZTERM_PANE`，連 TCP `127.0.0.1:47823`（或 socket-info.json 指定 port），寫 pane id
3. watcher 收到 → `wezterm cli get-text` → 分類 → codex → `wezterm cli send-text`
4. **跳過** `stable_count_required`；其他 gate 照常

**路徑 B：輪詢（fallback）**
1. 每 `poll_interval_seconds` 跑 `wezterm cli list --format json`
2. 對每個 pane 跑 capture + classify（**無 PID pre-filter**）
3. 連續 `stable_count_required` 次擷取相同才算靜止

**分類** (`classify()`)：
- `working`：title 是 Braille spinner（U+2800–U+28FF），或畫面尾段含 `esc to interrupt` / `(ctrl+o to expand)` → 跳過
- `menu`：尾段有 `❯ 1.`、`❯ 2.` 選單
- `input`：底部 `─...─` + `❯ ` 空輸入 + `─...─` 輸入框
- `other`：以上皆非 → 跳過（非 Claude pane、外部 shell pane 等）

只有 `input` 與 `menu` 送 codex。

**Codex 回傳** (`response_schema.json` 強制)：

```json
{ "action": "text" | "key" | "enter" | "skip", "value": "..." }
```

- `text` → `wezterm cli send-text --no-paste --pane-id N` payload `<text>\r`（單次 call）
- `key` → 同上，payload `<digit>\r`
- `enter` → 同上，payload `\r`
- `skip` → 不動，記錄

送出前**再抓一次**畫面比對；若已被人動過就 abort。`--no-paste` 是必要的，預設 bracketed-paste 模式會被 Claude Code 當作貼上資料而非按鍵。

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

要瘦身 forensic 檔，用 `log_pane_ids` 鎖定特定 pane：

```toml
log_pane_ids = [18]                # 或 ["18"] 或歷史相容 ["%18"]
```

或 env var：`$env:WATCHER_LOG_PANE_IDS = "18,19"`（逗號分隔）。主 `watcher.log` 不受影響，仍全量記錄。`log_enabled = false` 仍是總開關。

---

## 疑難排解

| 症狀 | 修法 |
|------|------|
| log 沒出現 `hook recv pane=N` | `claude --debug` 看 hook 是否註冊；確認 `/plugin list` 有 watcher |
| `hook event for unknown pane N` | 重開 Claude Code |
| hook timeout | 確認 daemon 還活著（`pwsh scripts\watcher-daemon.ps1 status`）、`~/.watcher/socket-info.json` 還在 |
| `codex: command not found` | `Get-Command codex` + `codex login status`，確認 PATH 含 nodejs 目錄 |
| `wezterm cli list` 失敗 | 確認 WezTerm GUI 正在跑（mux server 需要 GUI 啟動） |
| watcher 偵測不到 pane | `uv run watcher.py --once` 看分類；確認該 pane 真的在跑 Claude Code 且處於 `input`/`menu` 狀態 |
| killswitch 一直跳 | 調高 `per_pane_cooldown_seconds` 或降 `max_responses_per_window`，跳了**必須重啟 daemon** |
| codex 一直回 `skip` | 開 log 看 `logs/triggers/*.json`，常因提示太保守 |
| Port 47823 被占用 | daemon 會自動嘗試 +1..+10；查 `~/.watcher/socket-info.json` 看實際 port |

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
├── scripts/
│   ├── watcher-daemon.ps1   # daemon start/stop/status（PowerShell）
│   ├── install-hook.py      # 自動寫 settings.json
│   ├── milestone-toggle.py  # /watcher:milestone-* 後端
│   ├── release.sh           # pre-push 自動版號 bump（bash via git-for-windows）
│   └── git-hooks/pre-push
├── watcher.py               # daemon 本體（plugin 不啟動，要自己跑）
├── config.toml
└── response_schema.json
```

---

## 已知限制

- 沒有 Windows Service / 開機自動啟動（要手動 `watcher-daemon.ps1 start`）
- Codex 看不到 project 脈絡（只看單一 pane 畫面）
- Persona 內嵌在 `PROMPT_TEMPLATE`，不支援多 persona 切換
- WezTerm GUI 必須先啟動，daemon 才能透過 `wezterm cli` 與其溝通
- `wezterm cli get-text` 對 Claude Code 的 alt-screen 只看得到 viewport（~40-50 行），無法回溯更早的 scrollback；對 classify() 已足夠，但 forensic audit 看不到完整對話歷史
