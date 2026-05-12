# watcher

監聽 WSL2 tmux 裡所有正在跑 Claude Code 的 pane，偵測到 Claude 停在「等使用者輸入」狀態時，把畫面餵給 Codex CLI 決定怎麼回，再自動 `tmux send-keys` 把答案送回去。

```
                      ┌─► Claude Code Stop hook ─► unix socket ─► watcher ─┐
tmux pane (claude) ───┤                                                    ├─► codex exec ─► tmux send-keys
                      └─► watcher 每 3 分鐘輪詢 (fallback) ─────────────────┘
```

主觸發是 Claude Code 的 **Stop hook**（即時、零輪詢延遲）；輪詢只當 hook 沒涵蓋到的情境（例如 mid-task 權限確認可能不觸發 Stop hook）的安全網。

---

## 環境需求

- WSL2（Linux 端，本機驗證為 Ubuntu）
- `tmux` ≥ 3.x
- `uv`（Python 套件/環境管理器）
- Node.js（給 codex CLI 用）
- ChatGPT 訂閱（codex CLI 走訂閱授權）

> 注意：codex CLI 必須裝在 WSL2 端，**不要**從 Windows 端 `cmd.exe` 借用。WSL interop 每次冷啟動 ~200ms、跨界編碼也容易壞。

---

## 一次性安裝

### 1. 安裝 Node.js（如果還沒裝）

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
exec $SHELL          # 重載 shell 讓 nvm 上線
nvm install --lts
node --version       # 應印出 v20.x 或更新
```

### 2. 安裝 Codex CLI

```bash
npm i -g @openai/codex
codex --version
```

### 3. 用 ChatGPT 訂閱登入 codex

```bash
codex login          # 會開瀏覽器走 OAuth，登入你訂閱的 ChatGPT 帳號
codex login status   # 應該 exit 0；token 會存到 ~/.codex/auth.json
```

之後 `codex exec` 都會自動沿用這個登入。

### 4. 同步 watcher 的 Python 環境

```bash
cd /home/j7/DEV/watcher
uv sync              # 建立 .venv，安裝（無第三方）依賴
```

### 5. 安裝 Claude Code Stop hook（主觸發路徑）

#### hook 在做什麼

`hooks/claude-stop-notify.py` 是給 Claude Code 在「assistant turn 結束」時呼叫的小腳本。流程：

```
Claude Code turn 結束
        │
        ▼
  執行 claude-stop-notify.py
        │
   ┌────┴────┐  讀 $TMUX_PANE (例如 "%3")
   │         │
   │   socket 存在？──no──► 直接 exit 0（watcher 沒跑，本來就不該動）
   │         │
   │        yes
   │         │
   │   寫一行 pane id 到 unix socket → exit 0
   ▼
watcher 收到 → 抓畫面 → 分類 → (若是 input/menu) → codex → send-keys
```

特性：

- 純 stdlib Python，不需要 `pip install` 任何東西。
- 永遠 `exit 0`，連 socket 不存在、不在 tmux 裡、寫入失敗都不會卡住 Claude Code 的 turn。
- 1 秒 socket timeout，避免 watcher hang 住影響 Claude 的回應流。

#### 安裝（編輯 `~/.claude/settings.json`）

把下列 `Stop` hook 加進去；如果已經有其他 hook，合併進現有 `hooks` 物件即可：

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/home/j7/DEV/watcher/hooks/claude-stop-notify.py"
          }
        ]
      }
    ]
  }
}
```

注意事項：

- `command` 必須是**絕對路徑**。如果你把專案 clone 到別的位置，記得改這條路徑。
- 腳本要有執行權限：`chmod +x /home/j7/DEV/watcher/hooks/claude-stop-notify.py`（這個 repo 已經帶 +x，clone 後應該不用再設）。
- `matcher: "*"` 代表所有 Stop 事件都觸發。watcher 端會分類，所以放最寬即可。
- 設定改完**不用重啟** Claude Code 本身，但**正在跑的對話**要結束（或重開）才會吃到新設定。

#### 驗證 hook 有裝對

1. 先確認 watcher 在跑且 socket 已建立：

   ```bash
   ls -la "${XDG_RUNTIME_DIR:-/tmp}/watcher-$USER.sock"
   # 應看到 srw-------（socket，0600）
   ```

2. 把 watcher 開在**前景**並看 stderr（預設 `log_enabled=false` 不寫檔，stderr 才看得到）：

   ```bash
   cd /home/j7/DEV/watcher && uv run watcher.py --dry-run
   ```

   或者只為了驗證這一次開檔案 log：

   ```bash
   WATCHER_LOG_ENABLED=true uv run watcher.py --dry-run
   tail -f /home/j7/DEV/watcher/logs/watcher.log   # 另開一個 pane
   ```

3. 在**另一個** tmux pane 跑 Claude Code，隨便給它一句話讓它回完一個 turn。

4. watcher 那邊應該在 Claude 結束 turn 後**幾秒內**印出：

   ```
   evaluate 0:0.0 class=input src=hook — scheduling handler
   ```

   `src=hook` 就是成功；若只看到 `src=poll`（要等到 ≥3 分鐘才出現），表示 hook 沒生效。

#### hook 沒生效的常見原因

| 症狀 | 可能原因 | 修法 |
|------|---------|------|
| watcher log 從沒出現 `src=hook` | settings.json 沒讀到 / 路徑寫錯 | `claude --debug` 看啟動有沒有抱怨 settings；確認絕對路徑可執行 |
| 出現 `hook event for unknown pane %N` | Claude Code 跑在外層 tmux session 但 pane 已被換掉 | 重新開 Claude Code，pane_id 會更新 |
| Claude Code 抱怨 hook timeout | socket 不存在或 watcher 卡住 | 確認 watcher 還活著、socket 檔還在 |
| 完全沒反應且 watcher 也活著 | 不在 tmux 裡跑 Claude Code | 用 `tmux` 包起來再開 |

#### 自訂 socket 路徑（多使用者 / 容器 / 多 watcher 並存）

預設 socket 路徑：`$XDG_RUNTIME_DIR/watcher-$USER.sock`（fallback `/tmp/watcher-$USER.sock`）。

如果你要改：

1. 在 `config.toml` 設 `socket_path = "/custom/path.sock"`，**或**啟動時用 `WATCHER_SOCKET_PATH=/custom/path.sock uv run watcher.py`。
2. 同樣的路徑也要讓 hook 知道 — 在 `~/.claude/settings.json` 把 hook command 改成：

   ```json
   { "type": "command",
     "command": "env WATCHER_SOCKET_PATH=/custom/path.sock /home/j7/DEV/watcher/hooks/claude-stop-notify.py" }
   ```

   hook 腳本會優先讀 `WATCHER_SOCKET_PATH` 環境變數。

#### 暫時停用 / 永久解除

- **停 hook 通道但保留輪詢**：在 `config.toml` 設 `socket_enabled = false`，或啟動加 `WATCHER_SOCKET_ENABLED=false`。watcher 不會建 socket，hook 連不上自動 no-op，輪詢繼續跑。
- **整個解除**：把 `~/.claude/settings.json` 裡的 `Stop` hook 區塊砍掉即可，腳本本身留著沒影響。

---

## 啟動

watcher 是常駐 daemon，有三種跑法：

| 指令 | 用途 |
|------|------|
| `uv run watcher.py --once` | 跑一輪偵測就結束，**不**呼叫 codex、**不** send-keys。第一次驗證 pane 偵測有沒有抓對。 |
| `uv run watcher.py --dry-run` | 完整迴圈會呼叫 codex 做判斷，但**不** send-keys。建議搭配 `WATCHER_LOG_ENABLED=true` 一起跑，這樣可以看 `logs/triggers/*.json` 確認 codex 給的答案合理後再切正式。 |
| `uv run watcher.py` | **正式模式**：偵測到就自動 send-keys。 |

### 建議的常駐方式

開一個專用 tmux pane 跑：

```bash
tmux new -s watcher 'cd /home/j7/DEV/watcher && uv run watcher.py'
```

或背景 + log 重導向：

```bash
cd /home/j7/DEV/watcher
nohup uv run watcher.py >/dev/null 2>&1 &
```

watcher 不會把自己誤判為 claude pane（透過 `pane_current_command == "claude"` 過濾）。

### 停止

- 前景跑：Ctrl-C
- nohup 背景：`pkill -f 'watcher.py'`

watcher 有處理 SIGINT/SIGTERM，會乾淨退出。

---

## 設定

### 快速指引

watcher 讀設定的優先序（高 → 低）：

```
WATCHER_<KEY> 環境變數   >   config.toml   >   程式內建預設值
```

兩種途徑可以混用：永久改吃 TOML、暫時改吃 env。

| 想做的事 | 怎麼做 |
|---------|--------|
| 永久改某個值 | 編輯 `/home/j7/DEV/watcher/config.toml`，存檔後**重啟 watcher** |
| 只改這次跑 | `WATCHER_<KEY>=value uv run watcher.py` |
| 回到預設 | 把 `config.toml` 那行刪掉（或整個檔刪掉，會吃 `DEFAULTS`） |
| 確認載入的值 | 啟動時 watcher 會 log 出 `interval=...`、`hook socket listening at ...`；或跑 `uv run watcher.py --once` 看 |

設定**不是 live reload**：改完 `config.toml` 一定要重啟 watcher（Ctrl-C 後重跑，或 `pkill -f watcher.py && uv run watcher.py`）才會生效。

### 設定檔位置與格式

- 路徑：`/home/j7/DEV/watcher/config.toml`（與 `watcher.py` 同目錄）
- 格式：[TOML](https://toml.io/)。簡單規則：`key = value`，字串用 `"..."`、布林 `true/false`、數字直接寫、`#` 開頭是註解。
- 整個檔案**可以省略**或留空 — 缺檔時所有鍵走內建預設。
- 任何**單一行**也可省略，只列你想覆寫的鍵即可。

### 環境變數命名規則

把 TOML 鍵名整個轉大寫並前綴 `WATCHER_`：

| TOML 鍵 | 對應環境變數 |
|---------|-------------|
| `poll_interval_seconds` | `WATCHER_POLL_INTERVAL_SECONDS` |
| `socket_enabled` | `WATCHER_SOCKET_ENABLED` |
| `log_format` | `WATCHER_LOG_FORMAT` |

型別自動轉換：
- 布林：`1` / `true` / `yes` / `on`（大小寫不限）→ `true`；其他都是 `false`
- 整數 / 浮點：直接 parse；parse 失敗會 exit 1 並印錯誤
- 字串：原樣帶入

### 所有可調項目

| 鍵 | 預設值 | 說明 |
|----|--------|------|
| **輪詢 / 觸發** | | |
| `poll_interval_seconds` | `180` | fallback 輪詢間隔（秒）。Stop hook 是主觸發；這個是安全網，預設 3 分鐘 |
| `stable_count_required` | `2` | 輪詢路徑：連續幾次擷取畫面完全相同才算「靜止」、可觸發。hook 路徑會跳過此檢查 |
| `per_pane_cooldown_seconds` | `15` | 同一 pane 觸發後幾秒內不再觸發（hook 與 poll 共用） |
| `max_responses_per_window` | `5` | killswitch：時間窗內最多回應次數，超過該 pane 停用至重啟 |
| `response_window_minutes` | `5` | killswitch 的時間窗（分鐘） |
| **Stop hook 通道** | | |
| `socket_enabled` | `true` | 是否開啟 unix socket 接 Claude Code Stop hook |
| `socket_path` | `""` | 空字串 = 自動用 `$XDG_RUNTIME_DIR/watcher-$USER.sock`（fallback `/tmp/watcher-$USER.sock`）。要自訂時填絕對路徑 |
| **Codex** | | |
| `codex_binary` | `"codex"` | codex 可執行檔名 / 路徑 |
| `codex_timeout_seconds` | `90` | 單次 `codex exec` 最長等待時間 |
| **畫面擷取** | | |
| `capture_scrollback_lines` | `200` | 每次抓畫面含多少行 scrollback |
| `hr_min_length` | `50` | 偵測水平線（Claude 輸入框上下框線）時的最小 `─` 字元數 |
| **日誌** | | |
| `log_enabled` | `false` | 整體 log 開關。預設 `false`（畫面快照可能含 token / API key，預設不落地）。`true` 才會寫 `watcher.log` 與 `logs/triggers/*.json`；無論真假 stderr 都會即時輸出 |
| `log_format` | `"%(asctime)s %(levelname)s %(message)s"` | Python logging 格式字串（同時套用到 stderr 與 `watcher.log`） |
| `log_datefmt` | `"%Y-%m-%d %H:%M:%S"` | `%(asctime)s` 的時間格式 |
| `log_max_bytes` | `10_000_000` | 單一 `watcher.log` 大小上限（byte），超過自動 rotate |
| `log_backups` | `3` | rotate 後保留幾份備份（`watcher.log.1`、`.2`、…） |
| `log_retention_days` | `0` | `>0` 時每小時清掉 `logs/triggers/` 內 mtime 超過 N 天的檔；`0` = 不依日期清，靠手動 |

### 範例 1：把預設都收進 `config.toml`

```toml
# config.toml — 完整預設值（任何一行省略都 OK，會吃內建預設）
poll_interval_seconds      = 180
stable_count_required      = 2
per_pane_cooldown_seconds  = 15
max_responses_per_window   = 5
response_window_minutes    = 5

socket_enabled             = true
socket_path                = ""

codex_binary               = "codex"
codex_timeout_seconds      = 90

capture_scrollback_lines   = 200
hr_min_length              = 50

log_enabled                = false
log_format                 = "%(asctime)s %(levelname)s %(message)s"
log_datefmt                = "%Y-%m-%d %H:%M:%S"
log_max_bytes              = 10_000_000
log_backups                = 3
log_retention_days         = 0
```

### 範例 2：完全只靠 hook，停掉輪詢

把 polling 拉超久（一天），等 hook 來：

```toml
poll_interval_seconds = 86400
```

### 範例 3：完全不裝 hook，純輪詢（每分鐘掃一次，連續 3 次穩定才觸發）

```toml
poll_interval_seconds = 60
stable_count_required = 3
socket_enabled        = false
```

### 範例 4：嚴 killswitch、保留 7 天 audit

```toml
max_responses_per_window = 3
log_retention_days       = 7
```

### 範例 5：開啟 log 來除錯（預設關閉）

預設 `log_enabled=false`，watcher 只輸出 stderr、不寫檔。要看歷史 / 留 audit trail 時開啟：

```toml
log_enabled        = true   # 開始寫 watcher.log + logs/triggers/*.json
log_retention_days = 7      # 順手設個過期清理，避免 audit 永久堆積
```

> 提醒：`logs/triggers/*.json` 會把當下整片 Claude pane 畫面存進去，內容可能含 token、檔案路徑、commit hash 等敏感資訊。確認 `logs/` 在 `.gitignore` 裡（這 repo 已預設加好），分享 log 前先過濾。

### 範例 6：用環境變數臨時覆寫（不動 config.toml）

```bash
# 這次跑開 log + 60 秒輪詢 + 自訂 log 格式
WATCHER_LOG_ENABLED=true \
WATCHER_POLL_INTERVAL_SECONDS=60 \
WATCHER_LOG_FORMAT="%(asctime)s %(message)s" \
uv run watcher.py

# 這次跑開 log 一輪做除錯
WATCHER_LOG_ENABLED=true uv run watcher.py

# 這次跑關掉 hook 通道（純輪詢）
WATCHER_SOCKET_ENABLED=false uv run watcher.py
```

### 範例 7：自訂 log 格式

```toml
# 加上 logger 名稱與行號，方便 debug
log_format  = "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d %(message)s"
log_datefmt = "%H:%M:%S"
```

> Killswitch 跳閘後，該 pane 會被「永久」停用到 daemon 重啟為止。這是為了防止 codex 跟 claude 互相鏈式對話爆走。

---

## 工作原理

### 偵測流程

watcher 有兩條觸發路徑，最後都會匯流到同一個 `evaluate_pane` 函式做分類與 gating：

**路徑 A：Stop hook（主）**
1. Claude Code 完成一個 turn → 觸發 Stop hook。
2. `claude-stop-notify.py` 讀 `$TMUX_PANE`，連到 watcher 的 unix socket，寫入 pane id。
3. watcher 收到後立刻 `tmux list-panes` 找到該 pane、抓畫面、分類。
4. **跳過** `stable_count_required` 檢查（hook 本身就是「停下來」的訊號），其他 gate（cooldown / killswitch / classification）照常。

**路徑 B：輪詢（fallback）**
1. 每 `poll_interval_seconds` 秒（預設 180）跑一次 `tmux list-panes -a`，過濾 `pane_current_command == "claude"`。
2. 對每個候選 pane 跑 `tmux capture-pane -p -S -200` 抓畫面。
3. 連續 `stable_count_required` 次（預設 2）擷取結果完全相同才算「靜止」。
4. 通過所有 gate 後才觸發。

**畫面分類** (`classify()`)：
- **working**：title 開頭是 Braille spinner 字元（U+2800–U+28FF），或畫面尾段含 `esc to interrupt` / `(ctrl+o to expand)` → 直接跳過。
- **menu**：尾段出現 `❯ 1.`、`❯ 2.` 之類的選單項。
- **input**：底部出現 `─...─` + `❯ ` 空輸入 + `─...─` 的輸入框。
- **other**：以上都不是。

只有 `input` 與 `menu` 會送進 codex。所以即使 Stop hook 在 Claude 還沒進到輸入框時誤觸發，watcher 也會分類為 `other` / `working` 直接放生。

### 決策流程

觸發後 watcher 把畫面套進固定 prompt 模板，呼叫：

```bash
codex exec \
  --ephemeral \                       # 每次乾淨 session（並行安全）
  --skip-git-repo-check \
  --ask-for-approval never \
  --sandbox read-only \
  -C /home/j7/DEV/watcher \
  --output-schema response_schema.json \
  --output-last-message <tmp> \
  "<prompt + 畫面>"
```

`--output-schema` 強制 codex 回傳合法 JSON，watcher 解析後得到：

```json
{ "action": "text" | "key" | "enter" | "skip", "value": "..." }
```

- `text` → `tmux send-keys -l <text>` 然後 Enter
- `key` → 送單一數字鍵 + Enter
- `enter` → 只送 Enter
- `skip` → 不做任何事，但留下審計記錄

送出前還會**再抓一次**畫面，跟觸發時的快照比對；若不一致（例如使用者已經自己敲了字）就 abort。

---

## 日誌與審計

**預設 `log_enabled = false`** — watcher 只輸出 stderr、不在硬碟留任何痕跡：
- `watcher.log` 不寫
- `logs/triggers/` 不寫（連目錄都不建）
- `codex-out-*.txt` 走系統 `/tmp` 並讀完即刪
- stderr 仍會即時輸出，方便前景觀察 / `nohup` 重導

這是出於安全考量：每次觸發都會把整片 Claude pane 畫面落地，內容可能含 token、API key、檔案路徑、commit hash 等資訊。

**手動開啟 `log_enabled = true`**（除錯、留 audit trail 時）：

```
logs/
├── watcher.log               主 log（rotate，預設 10MB × 3 份，由 log_max_bytes / log_backups 控制）
└── triggers/
    ├── 1715500000-3.json     每次觸發一份：含畫面快照 + codex 回覆 + 最終動作
    └── codex-out-*.txt       codex 寫出的 raw final message
```

`logs/` 目錄是 `0700`，且寫進 `.gitignore`。

設 `log_retention_days = N` 後，watcher 會在啟動時與之後每小時掃一次 `logs/triggers/`，刪掉 mtime 超過 N 天的檔案；`watcher.log` 本身則由 rotate 控制（不受日期清理影響）。

---

## 疑難排解

### `codex: command not found`

```bash
which codex
codex login status
```

確認 codex 裝在 WSL2 端（不是 Windows）。`uv run watcher.py` 跑出來的環境會繼承當前 shell 的 PATH，所以你的 shell 要能找得到 `codex`。

### watcher 都不觸發

跑 `uv run watcher.py --once` 看分類結果：

- 顯示 `class=working` → 是不是 Claude 還在轉（title 有 spinner / 畫面有 `esc to interrupt`）？等它停下來。
- 顯示 `class=other` → 多半是 Claude 不是停在標準輸入框；可以把 pane 內容貼到 issue 我再加 pattern。
- 完全沒列出 → `pane_current_command` 不是 `claude`（可能你 claude 是透過 wrapper script 啟動）。

### 觸發太頻繁 / killswitch 一直跳

調高 `per_pane_cooldown_seconds` 或降低 `max_responses_per_window`。Killswitch 跳了之後**必須重啟 daemon**才會解除（這是故意的）。

### codex 一直回 `skip`

先把 log 開起來才看得到原因（預設關閉）：`WATCHER_LOG_ENABLED=true uv run watcher.py`。然後打開 `logs/triggers/*.json` 看 `decision.value`（codex 給的 skip 理由）。常見原因：
- 畫面提示太模糊
- 含「destructive」字眼（codex 被 prompt 教成保守）
- Codex 看不到任務脈絡（這個 daemon **故意**不傳 project 脈絡，避免它亂猜）

要更積極可以改 `watcher.py` 裡的 `PROMPT_TEMPLATE`，把「Be conservative」那段放寬。

### 想關掉某個 pane 的自動回應

最快：把那個 pane 的 `claude` 退出再重開（pane_id 會變），或直接停 watcher。
更乾淨：可以在 watcher.py 加 session/window 名稱黑名單，目前未實作。

---

## 安全限制（這版不做）

- 沒有 systemd unit / 開機自動啟動
- 沒有 web UI / 遠端控制
- Codex 看不到 project 脈絡（只看單一 pane 畫面）
- Persona 直接內嵌在 `PROMPT_TEMPLATE`，沒有支援多個 persona 切換

之後要加再說。
