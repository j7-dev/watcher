<#
.SYNOPSIS
  Start / stop / status the watcher daemon by spawning it inside a dedicated
  WezTerm pane (replaces the tmux-based watcher-daemon.sh from the WSL2 era).

.DESCRIPTION
  Dogfoods WezTerm as the daemon supervisor — the daemon is launched via
  `wezterm cli spawn --new-window` so the user can `wezterm cli activate-pane`
  to attach and watch live logs. State is tracked in
  $env:USERPROFILE\.watcher\daemon-state.json containing pane_id, the
  wezterm-gui pid that owns the pane, and StartTime for tamper detection.

  Actions:
    start    Spawn a new WezTerm window/pane running `uv run watcher.py`.
             No-op (exit 0) if a live daemon pane is detected.
    stop     `wezterm cli kill-pane` the daemon pane, remove state file.
    status   exit 0 with attach hint when live, exit 1 otherwise.

.PARAMETER Action
  start | stop | status. Default: status.

.NOTES
  Requires WezTerm GUI running (its `cli` subcommand needs the mux server).
  Requires PowerShell 7+ for ConvertFrom-Json -AsHashtable.
  Repo location resolved from $env:WATCHER_REPO or scripts/'s parent dir.
#>
#Requires -Version 7.0

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"

# ---- paths --------------------------------------------------------------------

$RepoRoot = if ($env:WATCHER_REPO) { $env:WATCHER_REPO } else { Split-Path $PSScriptRoot -Parent }
$StateDir = Join-Path $env:USERPROFILE ".watcher"
$StateFile = Join-Path $StateDir "daemon-state.json"

if (-not (Test-Path $RepoRoot)) {
    Write-Error "repo not found: $RepoRoot (set `$env:WATCHER_REPO to override)"
    exit 2
}

# ---- state helpers ------------------------------------------------------------

function Get-DaemonState {
    if (-not (Test-Path $StateFile)) { return $null }
    try {
        return Get-Content $StateFile -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
    } catch {
        Write-Warning ("daemon-state.json unreadable: {0}" -f $_.Exception.Message)
        return $null
    }
}

function Set-DaemonState {
    param([hashtable]$State)
    if (-not (Test-Path $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    }
    $tmp = "$StateFile.tmp"
    $State | ConvertTo-Json -Depth 10 | Out-File $tmp -Encoding utf8 -NoNewline
    Move-Item $tmp $StateFile -Force
}

function Remove-DaemonState {
    Remove-Item $StateFile -Force -ErrorAction SilentlyContinue
}

# ---- liveness check -----------------------------------------------------------

function Test-DaemonAlive {
    param([hashtable]$State)
    if (-not $State) { return $false }

    # 1. The wezterm-gui process recorded in state must still exist AND match
    #    the recorded StartTime — guards against PID recycling after a wezterm
    #    restart that would otherwise let us mistake a fresh wezterm-gui for
    #    the original supervisor.
    #
    # IMPORTANT: ConvertFrom-Json -AsHashtable auto-parses ISO timestamp strings
    # into [DateTime] objects, so $State.wezterm_started_at may be either a
    # string (no parse) or DateTime (parsed). Compare via [DateTime] coercion
    # of both sides to dodge the string-vs-DateTime locale-format trap that
    # caused the v0.1.x daemon-state liveness check to always fail on Windows.
    try {
        $p = Get-Process -Id $State.wezterm_pid -ErrorAction Stop
        $recorded = [DateTime]$State.wezterm_started_at
        # ProcessStartTime can vary by microseconds across calls; allow 1s slop.
        $delta = [Math]::Abs(($p.StartTime - $recorded).TotalSeconds)
        if ($delta -gt 1) {
            return $false
        }
    } catch {
        return $false
    }

    # 2. The daemon's wezterm pane must still be enumerable. wezterm cli kill-pane
    #    and manual window-close both make the pane vanish from list output.
    try {
        $panes = wezterm cli list --format json | ConvertFrom-Json
    } catch {
        return $false
    }
    return [bool]($panes | Where-Object { [int]$_.pane_id -eq [int]$State.pane_id })
}

# ---- attach hint --------------------------------------------------------------

function Write-AttachHint {
    param([hashtable]$State)
    Write-Host ("attach: wezterm cli activate-pane --pane-id {0}" -f $State.pane_id)
    $info = Join-Path $env:USERPROFILE ".watcher\socket-info.json"
    if (Test-Path $info) {
        try {
            $i = Get-Content $info -Raw | ConvertFrom-Json
            Write-Host ("socket: {0}:{1}" -f $i.host, $i.port)
        } catch {
            Write-Host "socket: info file unreadable"
        }
    } else {
        Write-Host "socket: absent (daemon not yet bound or already stopped)"
    }
    Write-Host ("repo:   {0}" -f $State.repo)
    Write-Host ("started: {0}" -f $State.started_at)
}

# ---- actions ------------------------------------------------------------------

function Invoke-Start {
    $existing = Get-DaemonState
    if (Test-DaemonAlive -State $existing) {
        Write-Host ("already running (pane_id={0})" -f $existing.pane_id)
        Write-AttachHint -State $existing
        return 0
    }
    if ($existing) {
        Write-Host "clearing stale state file from previous run"
        Remove-DaemonState
    }

    # wezterm cli spawn returns the new pane id on stdout. We launch pwsh
    # with -NoExit so the user can poke around the daemon's pane after stop,
    # and use the call operator (&) so quoting survives spaces in repo paths.
    $repoArg = $RepoRoot.Replace('\', '/')
    $cmd = "uv run watcher.py"
    $spawnOutput = wezterm cli spawn --new-window --workspace watcher `
        --cwd "$repoArg" -- pwsh -NoExit -Command $cmd 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Error ("wezterm cli spawn failed: {0}" -f ($spawnOutput -join "`n"))
        return 2
    }
    $paneIdStr = ($spawnOutput | Select-Object -Last 1).ToString().Trim()
    $paneId = [int]$paneIdStr

    # Identify the wezterm-gui supervisor process. Heuristic: pick the
    # newest-started wezterm-gui — works in the common single-instance setup
    # AND covers the multi-instance case where the most recent spawn produced
    # the window we just created.
    $wez = Get-Process -Name "wezterm-gui" -ErrorAction SilentlyContinue |
           Sort-Object StartTime -Descending |
           Select-Object -First 1
    if (-not $wez) {
        Write-Warning "wezterm-gui process not found — daemon spawned but state tracking degraded"
        $wezPid = 0
        $wezStart = ""
    } else {
        $wezPid = $wez.Id
        $wezStart = $wez.StartTime.ToString("o")
    }

    $state = @{
        pane_id            = $paneId
        wezterm_pid        = $wezPid
        wezterm_started_at = $wezStart
        started_at         = (Get-Date).ToString("o")
        repo               = $RepoRoot
    }
    Set-DaemonState -State $state
    Write-Host ("started (pane_id={0}, wezterm_pid={1}, repo={2})" -f $paneId, $wezPid, $RepoRoot)
    Write-AttachHint -State $state
    return 0
}

function Invoke-Stop {
    $state = Get-DaemonState
    $paneKilled = $false

    # Step 1: if state file points to a live pane, kill that pane (graceful path).
    if ($state -and (Test-DaemonAlive -State $state)) {
        try {
            wezterm cli kill-pane --pane-id $state.pane_id 2>&1 | Out-Null
            $paneKilled = $true
        } catch {
            Write-Warning ("wezterm cli kill-pane failed: {0}" -f $_.Exception.Message)
        }
    }

    # Step 2: sweep all orphan python procs running `watcher.py`. Multiple
    # restarts where state went out of sync with reality can strand prior
    # daemons; this catch-all guarantees no zombie writes config-cached audit
    # files after `stop`. Skip filtering by repo path so cache-dir launches
    # also get cleaned (CommandLine on Windows includes the script path).
    $orphans = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                 Where-Object { $_.CommandLine -like '*watcher.py*' })
    foreach ($p in $orphans) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Host ("killed orphan python pid={0}" -f $p.ProcessId)
        } catch {
            Write-Warning ("could not kill pid={0}: {1}" -f $p.ProcessId, $_.Exception.Message)
        }
    }

    # Step 3: clear bookkeeping files so the next `start` has a clean slate.
    Remove-DaemonState
    Remove-Item (Join-Path $env:USERPROFILE ".watcher\socket-info.json") -Force -ErrorAction SilentlyContinue

    if (-not $state -and $orphans.Count -eq 0) {
        Write-Host "not running (no state file, no orphans)"
        return 0
    }
    if ($paneKilled) {
        Write-Host ("stopped (pane_id={0}, orphans={1})" -f $state.pane_id, $orphans.Count)
    } elseif ($state) {
        Write-Host ("stopped (stale state cleared, orphans={0})" -f $orphans.Count)
    } else {
        Write-Host ("stopped (no state file, orphans={0})" -f $orphans.Count)
    }
    return 0
}

function Invoke-Status {
    $state = Get-DaemonState
    if (-not $state) {
        Write-Host "daemon: not running"
        return 1
    }
    if (Test-DaemonAlive -State $state) {
        Write-Host ("daemon: running (pane_id={0})" -f $state.pane_id)
        Write-AttachHint -State $state
        return 0
    }
    Write-Host "daemon: not running (stale state)"
    return 1
}

# ---- dispatch -----------------------------------------------------------------

switch ($Action) {
    "start"  { exit (Invoke-Start) }
    "stop"   { exit (Invoke-Stop) }
    "status" { exit (Invoke-Status) }
}
