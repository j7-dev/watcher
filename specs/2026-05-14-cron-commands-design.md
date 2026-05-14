# /watcher:cron-* 命令設計

> 日期：2026-05-14
> 狀態：approved (brainstorm)
> 涉及：3 個 slash command + 4 個 script

## 目標

每個專案可獨立註冊一個 Windows Task Scheduler 排程，定期喚醒 `claude -p` 對該專案做開發狀態觀察，自動產生報告寫入 `<project>/specs/reports/`。

兩種觀察模式由 AI 自動判定：
1. 專案未上線 → 追蹤開發進度、下一步、阻塞點
2. 專案已上線 → 改進空間、潛在 bug、測試缺口、競爭力建議

## 命令清單

| Command | 作用 | 參數 |
|---|---|---|
| `/watcher:cron-setup [interval]` | 為目前 cwd 註冊 / 更新 task | interval 預設 `5h`，支援 `5` / `5h` / `2d` / `30m` |
| `/watcher:cron-status [--all]` | 顯示目前 cwd task 狀態；`--all` 列所有 watcher cron | — |
| `/watcher:cron-remove [--all]` | 移除目前 cwd task；`--all` 移除所有 watcher cron | — |

## 架構

### Task naming

```
ClaudeWatcher_<sha8>
```

`sha8 = sha256(<absolute project path normalized>)[:8]`

同路徑重複 setup 是 idempotent update（`schtasks /F` 強制覆蓋）。

### Task source of truth

schtasks 自身（**A. Minimal** 方案）— 無 registry JSON，避免雙寫漂移。

- status 用 `schtasks /query /tn ClaudeWatcher_<sha8> /fo list /v` 取單筆
- `--all` 用 `schtasks /query /fo csv /v` 後過濾 `TaskName ~ /\\ClaudeWatcher_/`

### Schedule

```
schtasks.exe /Create /TN <name> /SC <unit> /MO <n> /TR "<action>" /F /RL LIMITED /IT
```

- `/RL LIMITED`：用戶權限執行，不要 SYSTEM
- `/IT`：僅互動 session 跑（無人登入時跳過，呼應「不喚醒電腦」決策）
- `/F`：覆蓋同名 task（idempotent）

### Action

```
pwsh -NoProfile -WindowStyle Hidden -File "<PLUGIN_ROOT>\scripts\cron-runner.ps1" -ProjectPath "<abs>"
```

`PLUGIN_ROOT` 在 setup 階段解析成絕對路徑寫入 task action — 因 Task Scheduler 不會展開 `${CLAUDE_PLUGIN_ROOT}`。

### Runner（`scripts/cron-runner.ps1`）

1. `Set-Location <ProjectPath>`
2. 確保 `<ProjectPath>\specs\reports\` 存在；失敗 fallback 到 `.watcher-cron\reports\`
3. timestamp = `Get-Date -Format yyyy-MM-dd-HHmm`
4. 跑 `claude -p "<hardcoded prompt>"` 並 capture stdout + stderr
5. 寫入 `specs\reports\<timestamp>-watcher.md`
6. exit code != 0：報告檔附 `## Error` 區塊含 exit code

### Prompt template（硬編碼於 cron-runner.ps1）

```
你是專案守護者。本次自動喚醒任務：

1. 用 git log / README / package.json or composer.json or pyproject.toml / specs/ / CHANGELOG 判定專案狀態：
   - 「開發中」：尚未上線、版本 < 1.0、specs/ 有未完成項目、近期 commits 集中於 feature 開發、無 release tag
   - 「已上線」：有 release tag、版本 >= 1.0、CHANGELOG 有 production 紀錄、近期 commits 多為 fix / chore

2. 依狀態執行：
   - 開發中：列出目前進度（已完成 / 進行中 / 待辦）、下一步應做事項、阻塞點。
   - 已上線：列出可改進處、潛在 bug、測試覆蓋缺口、競爭力建議。

3. 輸出 markdown 報告，包含四節：
   - 判定狀態（開發中 / 已上線 + 依據）
   - 觀察結果
   - 建議行動
   - 優先順序

純觀察任務，不執行任何寫入操作、不修改檔案。
```

## Interval 解析

### Regex
```python
^(?P<num>\d+)(?P<unit>[hHdDmM])?$
```

### 對應

| 輸入 | 解析 | schtasks |
|---|---|---|
| 空 | 5h | `/SC HOURLY /MO 5` |
| `5` | 5h（裸數字→小時） | `/SC HOURLY /MO 5` |
| `5h` | 5 小時 | `/SC HOURLY /MO 5` |
| `2d` | 2 天 | `/SC DAILY /MO 2` |
| `30m` | 30 分鐘 | `/SC MINUTE /MO 30` |

### 範圍

schtasks 硬限制：

- minutes: 1–1439
- hours: 1–23（超過用 d）
- days: 1–365

超限拒絕並提示換算（`24h` → 「請改用 `1d`」）。

## 邊界處理

| 情境 | 行為 |
|---|---|
| `claude` 不在 PATH | setup 時 `where claude` 預檢，找不到則 abort + 提示 |
| 目錄無 `.git` | 仍允許 setup |
| `specs/reports/` 寫入失敗 | runner fallback 到 `.watcher-cron/reports/` |
| Task 已存在但用戶改 interval | `/F` 直接覆蓋 |
| 用戶手動刪除 task | status 回 "not registered" |
| wezterm/uv 全部不需要 | cron 系統完全獨立於 watcher daemon |

## 安全性

- runner 跑 `claude -p` 純讀取模式，prompt 明示「不修改檔案」
- task `/RL LIMITED` 用戶權限，不要 SYSTEM
- task `/IT` 互動 session-only，無 stealth 跑
- 報告檔可能含 git log / file content snippet — 建議 `.watcher-cron/` 加入 `.gitignore` 但 `specs/reports/` 由用戶自行決定

## 檔案佈局

```
.claude-plugin/plugin.json
commands/
  cron-setup.md          # 新增
  cron-status.md         # 新增
  cron-remove.md         # 新增
scripts/
  cron-setup.py          # 新增 — interval 解析 + schtasks /Create
  cron-status.py         # 新增 — schtasks /query 解析
  cron-remove.py         # 新增 — schtasks /Delete
  cron-runner.ps1        # 新增 — claude -p 呼叫 + 報告輸出
specs/
  2026-05-14-cron-commands-design.md  # 本檔
  reports/               # runner 寫入目標（gitignore）
```

## Out of scope（本次不做）

- registry JSON / 跨機同步
- 喚醒電腦 / wake-from-sleep
- 多種 prompt template（per-project override）
- 報告檔自動 archive / rotation
- 失敗時的 retry 機制（依賴 Task Scheduler 自己 next interval 重試）

## 後續路線

若用戶反饋需要 prompt 客製化 → 加入 `<project>/.watcher-cron/prompt.md` override（B 方案的最小子集）。

---

## Addendum 2026-05-14 #2 — GitHub Issue 整合

### 範圍擴充

除了寫 `specs/reports/`，runner 還要將 AI 發現的事項發佈到 GitHub Issue：

- 三種 label：`Bug` / `Feature` / `Task`
- 重複 finding 跳過（與既存 open issue 比對 signature）

### 決策

| 維度 | 決定 |
|---|---|
| Issue 建立執行者 | **Post-processor 剖析 + gh CLI**（cron-runner.ps1 內部） |
| 重複 finding 處理 | **Skip**（同 signature open issue 存在則略過） |
| 無 GitHub remote 時 | **Silent skip**（仍寫 report，附說明） |

### Prompt 擴充

要求 AI 在報告末附加機器可剖析區塊：

```
<!-- WATCHER_ISSUES_BEGIN -->
```json
[
  {
    "type": "Bug|Feature|Task",
    "signature": "lowercase-kebab-case-stable-slug",
    "title": "簡潔標題",
    "severity": "high|medium|low",
    "body": "完整描述"
  }
]
```
<!-- WATCHER_ISSUES_END -->
```

`signature` 規範：穩定 noun phrase、lowercase-kebab-case、**禁用**日期 / 時間 / 版本號 / 行號 / 檔案路徑 / commit hash — 確保跨 run 同概念產出同 signature。

### Issue 格式

- Title: `[Watcher][<Type>] <短標題>`
- Body 頂部嵌 HTML comment marker：`<!-- watcher-signature: <slug> -->`
- 含 source report 路徑回連

### Dedupe 邏輯

```
existing = { sig | sig in body of open issues, repo=<project repo>, limit=200 }
for finding in findings:
    if finding.signature in existing: skip
    else: gh issue create --label <Type>
```

只看 **open** issues — closed 同 signature 不算重複，允許 regression 被重新提報。

### Label 處理

`gh label create <Type>` 在 publish 前對 `Bug` / `Feature` / `Task` 三者各打一次，已存在則靜默忽略。預設色 `#BFD4F2`。

### gh CLI precheck

cron-setup 階段：

- `gh` 不在 PATH → 警告但不阻塞 setup
- `gh auth status` 失敗 → 警告但不阻塞 setup
- `gh repo view` 在 cwd 失敗（無 remote）→ 警告但不阻塞 setup

Runtime（cron-runner）：

- 三者任一失敗 → 跳過 issue publishing；report 仍寫，附 `## Issue publishing summary` 區塊註記原因

### 邊界

| 情境 | 行為 |
|---|---|
| AI 輸出無 issues block | runner 記 error 「no WATCHER_ISSUES block」，report 仍寫 |
| JSON 剖析失敗 | runner 記 error，report 仍寫 |
| finding 缺 signature | 跳過該 finding，記 error |
| finding type 不在白名單 | 跳過該 finding，記 error |
| 同一 run 內出現兩個相同 signature | 第一個建 issue 後加入 existing set，第二個被 skip |
| signature 漂移（AI 換字） | MVP 接受 — 用戶手動 close 即可避免後續再產 |

### Report 內 issue publishing summary

每份 report 末尾自動附加：

```
## Issue publishing summary
- Attempted : N
- Created   : N
- Skipped   : N (duplicates of open issues)
- Errors    : N

### Created / Skipped (dedup) / Errors  (依需要顯示)
```
