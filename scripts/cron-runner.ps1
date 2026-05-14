# cron-runner.ps1
# Invoked by Windows Task Scheduler. Runs `claude -p` against a project,
# writes the resulting markdown report into <project>/specs/reports/,
# parses a structured issues block from the AI output, and publishes
# new findings to GitHub as labeled issues (Bug / Feature / Task) while
# deduplicating against existing open issues.
#
# Designed to be self-contained: every failure mode is captured into the
# report file rather than thrown — Task Scheduler has no stderr sink.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectPath
)

$ErrorActionPreference = 'Continue'

# Magic marker line; do not change without updating the prompt below.
$IssuesBeginMarker = '<!-- WATCHER_ISSUES_BEGIN -->'
$IssuesEndMarker   = '<!-- WATCHER_ISSUES_END -->'
$SignaturePrefix   = 'watcher-signature:'
$KnownTypes        = @('Bug', 'Feature', 'Task')
$WatcherLabels     = @{ 'Bug' = 'Bug'; 'Feature' = 'Feature'; 'Task' = 'Task' }
$TitlePrefix       = '[Watcher]'

$Prompt = @"
你是專案守護者。本次自動喚醒任務分兩部分：

# 第一部分：撰寫觀察報告

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

# 第二部分：結構化 issues block

報告寫完後，在文件最末附加**機器可剖析**的 issues 區塊，格式如下（必須完全照樣）：

$IssuesBeginMarker
``````json
[
  {
    "type": "Bug" 或 "Feature" 或 "Task",
    "signature": "lowercase-kebab-case-stable-slug",
    "title": "簡潔標題（不含 [Watcher] 前綴，post-processor 會自己加）",
    "severity": "high" 或 "medium" 或 "low",
    "body": "完整描述，含背景、現象、影響、建議。可以多行。"
  }
]
``````
$IssuesEndMarker

規則（**重要**）：

- ``type`` 三選一：``Bug`` = 已知或潛在錯誤；``Feature`` = 新功能 / 改進；``Task`` = 重構 / 文件 / 測試 / 雜事。
- ``signature`` **必須穩定**：同一個概念在未來的 run 也要產生同樣的字串。
  - 用名詞短語、lowercase-kebab-case、不含日期、時間、版本號、行號、檔案路徑、commit hash。
  - 例：``null-check-missing-in-pane-loop``、``add-e2e-test-for-classify``、``refactor-config-loader``。
  - **不要**：``bug-2026-05-14-line-42``、``v0.1.7-issue``。
- ``title`` 短而精確；不要重複 type 字眼（type 已分類）。
- 沒有任何 finding 也要輸出空陣列 ``[]``。
- 不要在 begin / end marker 中間放任何非 JSON 文字。
- 整份報告純觀察任務；**不執行任何寫入操作、不修改任何檔案**。
"@

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Resolve-ReportDir {
    param([string] $Project)
    $primary = Join-Path $Project 'specs\reports'
    try {
        if (-not (Test-Path $primary)) {
            New-Item -ItemType Directory -Path $primary -Force | Out-Null
        }
        return $primary
    } catch {
        $fallback = Join-Path $Project '.watcher-cron\reports'
        if (-not (Test-Path $fallback)) {
            New-Item -ItemType Directory -Path $fallback -Force | Out-Null
        }
        return $fallback
    }
}

function Get-IssuesJson {
    param([string] $Markdown)
    $pattern = [regex]::Escape($IssuesBeginMarker) + '(?<inner>[\s\S]*?)' + [regex]::Escape($IssuesEndMarker)
    $m = [regex]::Match($Markdown, $pattern)
    if (-not $m.Success) { return $null }
    $inner = $m.Groups['inner'].Value
    # Strip optional ```json ... ``` fences
    $jsonMatch = [regex]::Match($inner, '```(?:json)?\s*(?<body>[\s\S]*?)```')
    if ($jsonMatch.Success) {
        return $jsonMatch.Groups['body'].Value.Trim()
    }
    return $inner.Trim()
}

function Test-GhAvailable {
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $gh) { return $false }
    # Auth sanity check — quiet, fast
    $authOut = & $gh.Source auth status 2>&1
    return ($LASTEXITCODE -eq 0)
}

function Get-RepoSlug {
    param([string] $Project)
    Push-Location -LiteralPath $Project
    try {
        $slug = & gh repo view --json nameWithOwner -q '.nameWithOwner' 2>$null
        if ($LASTEXITCODE -eq 0 -and $slug) { return $slug.Trim() }
        return $null
    } finally {
        Pop-Location
    }
}

function Ensure-Labels {
    param([string] $RepoSlug)
    foreach ($name in $KnownTypes) {
        # Idempotent: silently ignore "already exists"
        & gh label create $name --repo $RepoSlug --color BFD4F2 --description "Auto-created by watcher cron" 2>$null | Out-Null
    }
}

function Get-ExistingSignatures {
    param([string] $RepoSlug)
    $set = New-Object System.Collections.Generic.HashSet[string]
    $raw = & gh issue list --repo $RepoSlug --state open --limit 200 --json number,title,body 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $set }
    try {
        $issues = $raw | ConvertFrom-Json
    } catch {
        return $set
    }
    foreach ($it in $issues) {
        $body = if ($it.body) { [string] $it.body } else { '' }
        $rx = [regex]::Match($body, [regex]::Escape($SignaturePrefix) + '\s*(?<sig>[a-z0-9\-_]+)')
        if ($rx.Success) {
            [void] $set.Add($rx.Groups['sig'].Value.ToLower())
        }
    }
    return $set
}

function New-IssueBody {
    param(
        [string] $Type,
        [string] $Signature,
        [string] $Severity,
        [string] $Body,
        [string] $ReportPath
    )
    $reportNote = if ($ReportPath) { "Source report: ``$ReportPath``" } else { '' }
    $sev = if ($Severity) { $Severity } else { 'unspecified' }
    return @"
<!-- $SignaturePrefix $Signature -->

> **Auto-generated by watcher cron.** Type: ``$Type`` · Severity: ``$sev``
>
> $reportNote

$Body

---

_If this issue is no longer relevant, just close it — watcher will not re-create it as long as a closed issue with the same signature is excluded only when **open**. (Closed duplicates are intentionally allowed to surface regressions.)_
"@
}

function Publish-Issues {
    param(
        [string]   $Project,
        [string]   $JsonText,
        [string]   $ReportPath
    )
    $result = [pscustomobject]@{
        Attempted = 0
        Created   = @()
        Skipped   = @()
        Errors    = @()
    }
    if ([string]::IsNullOrWhiteSpace($JsonText)) {
        $result.Errors += 'no WATCHER_ISSUES block in AI output'
        return $result
    }
    try {
        $findings = $JsonText | ConvertFrom-Json
    } catch {
        $result.Errors += "issues JSON parse failed: $($_.Exception.Message)"
        return $result
    }
    if ($null -eq $findings) { return $result }
    if ($findings -isnot [System.Array]) { $findings = @($findings) }
    if ($findings.Count -eq 0) { return $result }

    if (-not (Test-GhAvailable)) {
        $result.Errors += 'gh CLI not on PATH or not authenticated; skipping issue publish'
        return $result
    }
    $repo = Get-RepoSlug -Project $Project
    if (-not $repo) {
        $result.Errors += 'no GitHub remote detected for project; skipping issue publish'
        return $result
    }
    Ensure-Labels -RepoSlug $repo
    $existing = Get-ExistingSignatures -RepoSlug $repo

    foreach ($f in $findings) {
        $result.Attempted++
        $type      = [string] $f.type
        $signature = ([string] $f.signature).Trim().ToLower()
        $title     = ([string] $f.title).Trim()
        $severity  = if ($f.severity) { [string] $f.severity } else { '' }
        $body      = if ($f.body) { [string] $f.body } else { '' }

        if (-not $signature) {
            $result.Errors += "finding missing signature: title='$title'"
            continue
        }
        if ($KnownTypes -notcontains $type) {
            $result.Errors += "finding has unknown type '$type' (signature=$signature); skipping"
            continue
        }
        if ($existing.Contains($signature)) {
            $result.Skipped += "$signature  (duplicate of existing open issue)"
            continue
        }

        $fullTitle = "$TitlePrefix" + "[$type] $title"
        $issueBody = New-IssueBody -Type $type -Signature $signature -Severity $severity -Body $body -ReportPath $ReportPath
        $bodyFile  = [System.IO.Path]::GetTempFileName()
        Set-Content -LiteralPath $bodyFile -Value $issueBody -Encoding UTF8

        Push-Location -LiteralPath $Project
        try {
            $createOut = & gh issue create --repo $repo --title $fullTitle --body-file $bodyFile --label $WatcherLabels[$type] 2>&1
            if ($LASTEXITCODE -eq 0) {
                $result.Created += "$signature  -> $($createOut.Trim())"
                [void] $existing.Add($signature)
            } else {
                $result.Errors += "gh issue create failed for $signature : $($createOut -join ' ')"
            }
        } finally {
            Pop-Location
            Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue
        }
    }
    return $result
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if (-not (Test-Path -LiteralPath $ProjectPath -PathType Container)) {
    exit 1
}

Set-Location -LiteralPath $ProjectPath

$reportDir  = Resolve-ReportDir -Project $ProjectPath
$timestamp  = Get-Date -Format 'yyyy-MM-dd-HHmm'
$reportPath = Join-Path $reportDir "$timestamp-watcher.md"

$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) {
    @"
# Watcher cron report — $timestamp

## Error

``claude`` CLI not found on PATH for the Task Scheduler session.

- Project: $ProjectPath

Install Claude Code and ensure ``claude.exe`` is on the system PATH, then
re-run ``/watcher:cron-setup`` from the project directory.
"@ | Set-Content -LiteralPath $reportPath -Encoding UTF8
    exit 2
}

$stdoutFile = [System.IO.Path]::GetTempFileName()
$stderrFile = [System.IO.Path]::GetTempFileName()
$exit       = 0

try {
    $proc = Start-Process -FilePath $claude `
        -ArgumentList @('-p', $Prompt) `
        -WorkingDirectory $ProjectPath `
        -NoNewWindow `
        -PassThru `
        -Wait `
        -RedirectStandardOutput $stdoutFile `
        -RedirectStandardError $stderrFile
    $exit = $proc.ExitCode
} catch {
    $exit = 99
    Set-Content -LiteralPath $stderrFile -Value $_.Exception.Message -Encoding UTF8
}

$stdout = ''
$stderr = ''
if (Test-Path $stdoutFile) { $stdout = Get-Content -Raw -LiteralPath $stdoutFile }
if (Test-Path $stderrFile) { $stderr = Get-Content -Raw -LiteralPath $stderrFile }
Remove-Item -LiteralPath $stdoutFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $stderrFile -Force -ErrorAction SilentlyContinue

# Issue publishing happens regardless of claude exit code, so long as we got
# any output that contains an issues block. If exit != 0 and there is no
# usable block, publishIssues will just record errors.
$issuesJson = Get-IssuesJson -Markdown $stdout
$pub        = Publish-Issues -Project $ProjectPath -JsonText $issuesJson -ReportPath $reportPath

$header = @"
<!--
generated by: $($MyInvocation.MyCommand.Path)
project:      $ProjectPath
claude:       $claude
timestamp:    $timestamp
exit_code:    $exit
-->

"@

$issuesSection = @"


---

## Issue publishing summary

- Attempted : $($pub.Attempted)
- Created   : $($pub.Created.Count)
- Skipped   : $($pub.Skipped.Count) (duplicates of open issues)
- Errors    : $($pub.Errors.Count)
"@
if ($pub.Created.Count -gt 0) {
    $issuesSection += "`n`n### Created`n"
    foreach ($c in $pub.Created) { $issuesSection += "- $c`n" }
}
if ($pub.Skipped.Count -gt 0) {
    $issuesSection += "`n### Skipped (dedup)`n"
    foreach ($s in $pub.Skipped) { $issuesSection += "- $s`n" }
}
if ($pub.Errors.Count -gt 0) {
    $issuesSection += "`n### Errors`n"
    foreach ($e in $pub.Errors) { $issuesSection += "- $e`n" }
}

$body = $stdout
if ($exit -ne 0) {
    $errBlock = if ($stderr.Trim()) { $stderr } else { '(empty)' }
    $body += @"


---

## Error

``claude -p`` exited with code **$exit**.

``````
$errBlock
``````
"@
}

($header + $body + $issuesSection) | Set-Content -LiteralPath $reportPath -Encoding UTF8

exit $exit
