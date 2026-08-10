param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("export", "import")]
    [string]$Mode,
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }),
    [string]$Out,
    [string]$From,
    [switch]$Yes,
    [switch]$RefreshSidebar,
    [switch]$Restart,
    [switch]$AllowPlatformMismatch
)

$ErrorActionPreference = "Stop"

function Fail {
    param([string]$Message)
    throw "codex-sidebar-sync: $Message"
}

function Get-UtcStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
}

function Get-HostNameText {
    if ($env:COMPUTERNAME) { return $env:COMPUTERNAME }
    return [System.Net.Dns]::GetHostName()
}

function Expand-LocalPath {
    param([string]$PathText)
    if (-not $PathText) { return $null }
    $expanded = [Environment]::ExpandEnvironmentVariables($PathText)
    if ($expanded -eq "~") {
        $expanded = $HOME
    }
    elseif ($expanded.StartsWith("~\")) {
        $expanded = Join-Path $HOME $expanded.Substring(2)
    }
    elseif ($expanded.StartsWith("~/")) {
        $expanded = Join-Path $HOME $expanded.Substring(2)
    }
    return [System.IO.Path]::GetFullPath($expanded)
}

function Copy-FileIfExists {
    param(
        [string]$Src,
        [string]$Dst
    )
    if (Test-Path -LiteralPath $Src -PathType Leaf) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dst) | Out-Null
        Copy-Item -LiteralPath $Src -Destination $Dst -Force
    }
}

function Copy-DirectoryIfExists {
    param(
        [string]$Src,
        [string]$Dst
    )
    if (-not (Test-Path -LiteralPath $Src -PathType Container)) { return }
    if (Test-Path -LiteralPath $Dst) {
        Remove-Item -LiteralPath $Dst -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Dst | Out-Null
    Get-ChildItem -LiteralPath $Src -Force | Copy-Item -Destination $Dst -Recurse -Force
}

function Invoke-PythonSqliteBackup {
    param(
        [string]$Src,
        [string]$Dst
    )

    $script = @'
import pathlib
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
pathlib.Path(dst).parent.mkdir(parents=True, exist_ok=True)
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
try:
    with target:
        source.backup(target)
finally:
    target.close()
    source.close()
'@

    $tmp = [System.IO.Path]::ChangeExtension([System.IO.Path]::GetTempFileName(), ".py")
    Set-Content -LiteralPath $tmp -Value $script -Encoding UTF8
    try {
        $candidates = @(
            @{ Command = "python3"; Args = @() },
            @{ Command = "python"; Args = @() },
            @{ Command = "py"; Args = @("-3") }
        )
        foreach ($candidate in $candidates) {
            if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
            & $candidate.Command @($candidate.Args) $tmp $Src $Dst
            if ($LASTEXITCODE -eq 0) { return $true }
        }
    }
    finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    return $false
}

function Invoke-SqliteCliBackup {
    param(
        [string]$Src,
        [string]$Dst
    )
    $sqlite = Get-Command sqlite3 -ErrorAction SilentlyContinue
    if (-not $sqlite) { return $false }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dst) | Out-Null
    $escapedDst = $Dst.Replace("'", "''")
    & $sqlite.Source $Src ".backup '$escapedDst'"
    return ($LASTEXITCODE -eq 0)
}

function Backup-SqliteIfExists {
    param(
        [string]$Src,
        [string]$Dst
    )
    if (-not (Test-Path -LiteralPath $Src -PathType Leaf)) { return }
    if (Test-Path -LiteralPath $Dst) {
        Remove-Item -LiteralPath $Dst -Force
    }
    if (Invoke-PythonSqliteBackup -Src $Src -Dst $Dst) { return }
    if (Invoke-SqliteCliBackup -Src $Src -Dst $Dst) { return }
    Fail "could not back up live SQLite database; install Python or sqlite3 and retry: $Src"
}

function Restore-SqliteIfExists {
    param(
        [string]$Src,
        [string]$Dst
    )
    if (Test-Path -LiteralPath $Src -PathType Leaf) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dst) | Out-Null
        Copy-Item -LiteralPath $Src -Destination $Dst -Force
    }
}

function Write-Manifest {
    param(
        [string]$Bundle,
        [string]$CodexHomePath,
        [string]$ModeName
    )
    $manifest = [ordered]@{
        schema_version = "1.0"
        kind = "codex_sidebar_state_bundle"
        mode = $ModeName
        created_at = (Get-UtcStamp)
        hostname = (Get-HostNameText)
        platform = "windows"
        source_codex_home = $CodexHomePath
    }
    $manifest | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $Bundle "manifest.json") -Encoding UTF8
}

function Export-Bundle {
    param(
        [string]$CodexHomePath,
        [string]$Bundle
    )
    if (-not (Test-Path -LiteralPath $CodexHomePath -PathType Container)) {
        Fail "Codex home not found: $CodexHomePath"
    }
    New-Item -ItemType Directory -Force -Path $Bundle | Out-Null

    Copy-FileIfExists (Join-Path $CodexHomePath ".codex-global-state.json") (Join-Path $Bundle ".codex-global-state.json")
    Copy-FileIfExists (Join-Path $CodexHomePath "session_index.jsonl") (Join-Path $Bundle "session_index.jsonl")
    Copy-FileIfExists (Join-Path $CodexHomePath "external_agent_session_imports.json") (Join-Path $Bundle "external_agent_session_imports.json")
    Copy-FileIfExists (Join-Path $CodexHomePath "config.toml") (Join-Path $Bundle "config.toml")

    Backup-SqliteIfExists (Join-Path $CodexHomePath "state_5.sqlite") (Join-Path $Bundle "state_5.sqlite")
    Backup-SqliteIfExists (Join-Path $CodexHomePath "sqlite\state_5.sqlite") (Join-Path $Bundle "sqlite\state_5.sqlite")
    Backup-SqliteIfExists (Join-Path $CodexHomePath "logs_2.sqlite") (Join-Path $Bundle "logs_2.sqlite")
    Backup-SqliteIfExists (Join-Path $CodexHomePath "memories_1.sqlite") (Join-Path $Bundle "memories_1.sqlite")
    Backup-SqliteIfExists (Join-Path $CodexHomePath "goals_1.sqlite") (Join-Path $Bundle "goals_1.sqlite")

    Copy-DirectoryIfExists (Join-Path $CodexHomePath "sessions") (Join-Path $Bundle "sessions")
    Copy-DirectoryIfExists (Join-Path $CodexHomePath "archived_sessions") (Join-Path $Bundle "archived_sessions")
    Copy-DirectoryIfExists (Join-Path $CodexHomePath "ambient-suggestions") (Join-Path $Bundle "ambient-suggestions")
    Copy-DirectoryIfExists (Join-Path $CodexHomePath "attachments") (Join-Path $Bundle "attachments")
    Copy-DirectoryIfExists (Join-Path $CodexHomePath "generated_images") (Join-Path $Bundle "generated_images")

    Write-Manifest -Bundle $Bundle -CodexHomePath $CodexHomePath -ModeName "export"
    Write-Host "Exported Codex sidebar/session bundle: $Bundle"
}

function Backup-Target {
    param([string]$CodexHomePath)
    $backupDir = Join-Path $CodexHomePath ("backups\sidebar-state-sync-" + (Get-UtcStamp))
    Export-Bundle -CodexHomePath $CodexHomePath -Bundle $backupDir | Out-Null
    return $backupDir
}

function Test-SqliteIntegrity {
    param([string]$DbPath)
    if (-not (Test-Path -LiteralPath $DbPath -PathType Leaf)) { return }

    $script = @'
import sqlite3
import sys

db = sys.argv[1]
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
try:
    row = conn.execute("pragma integrity_check").fetchone()
    print(row[0] if row else "")
finally:
    conn.close()
'@

    $tmp = [System.IO.Path]::ChangeExtension([System.IO.Path]::GetTempFileName(), ".py")
    Set-Content -LiteralPath $tmp -Value $script -Encoding UTF8
    try {
        $candidates = @(
            @{ Command = "python3"; Args = @() },
            @{ Command = "python"; Args = @() },
            @{ Command = "py"; Args = @("-3") }
        )
        foreach ($candidate in $candidates) {
            if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
            $result = & $candidate.Command @($candidate.Args) $tmp $DbPath
            if ($LASTEXITCODE -eq 0) {
                if (($result | Select-Object -First 1) -ne "ok") {
                    Fail "state_5.sqlite integrity check failed: $result"
                }
                return
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }

    $sqlite = Get-Command sqlite3 -ErrorAction SilentlyContinue
    if ($sqlite) {
        $result = & $sqlite.Source $DbPath "pragma integrity_check;"
        if ($LASTEXITCODE -ne 0 -or (($result | Select-Object -First 1) -ne "ok")) {
            Fail "state_5.sqlite integrity check failed: $result"
        }
        return
    }

    Write-Warning "Skipped SQLite integrity check because neither Python nor sqlite3 is available."
}

function Refresh-SidebarState {
    param([string]$CodexHomePath)
    Test-SqliteIntegrity -DbPath (Join-Path $CodexHomePath "state_5.sqlite")
    $markerDir = Join-Path $CodexHomePath "backups\sidebar-state-sync-refresh"
    New-Item -ItemType Directory -Force -Path $markerDir | Out-Null
    $marker = [ordered]@{
        refreshed_at = (Get-UtcStamp)
        note = "Restart Codex Desktop to reload sidebar state."
    }
    $marker | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $markerDir "last-refresh.json") -Encoding UTF8
    Write-Host "Refreshed on-disk sidebar state and wrote marker: $markerDir\last-refresh.json"
}

function Get-CodexWriterProcesses {
    return @(
        Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ProcessName -in @("ChatGPT", "Codex", "codex", "codex-code-mode-host")
        }
    )
}

function Stop-CodexDesktop {
    $processes = Get-CodexWriterProcesses
    if ($processes) {
        $processes | Stop-Process -Force
        Start-Sleep -Seconds 2
        $remaining = Get-CodexWriterProcesses
        if ($remaining) {
            Fail "Codex writer processes are still active: $($remaining.Id -join ', ')"
        }
        Write-Host "Stopped Codex Desktop."
    }
}

function Start-CodexDesktop {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Codex\Codex.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\codex\Codex.exe"),
        (Join-Path $env:LOCALAPPDATA "codex\Codex.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }

    if ($candidates.Count -gt 0) {
        Start-Process -FilePath $candidates[0]
        Write-Host "Started Codex Desktop: $($candidates[0])"
        return
    }

    $command = Get-Command "Codex.exe" -ErrorAction SilentlyContinue
    if ($command) {
        Start-Process -FilePath $command.Source
        Write-Host "Started Codex Desktop: $($command.Source)"
        return
    }

    Write-Warning "Restart requested, but Codex.exe was not found. Start Codex Desktop manually."
}

function Import-Bundle {
    param(
        [string]$CodexHomePath,
        [string]$Bundle,
        [bool]$DoRefresh,
        [bool]$DoRestart,
        [bool]$DoAllowPlatformMismatch
    )
    if (-not (Test-Path -LiteralPath $Bundle -PathType Container)) {
        Fail "bundle directory not found: $Bundle"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Bundle "manifest.json") -PathType Leaf)) {
        Fail "bundle manifest not found: $Bundle\manifest.json"
    }
    $manifest = Get-Content -LiteralPath (Join-Path $Bundle "manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$manifest.kind -ne "codex_sidebar_state_bundle") {
        Fail "unrecognized bundle manifest kind: $($manifest.kind)"
    }
    $sourcePlatform = [string]$manifest.platform
    if (-not $sourcePlatform) {
        $sourceHome = [string]$manifest.source_codex_home
        if ($sourceHome -match '^/') {
            $sourcePlatform = if ($sourceHome -match '^/Users/') { "macos" } else { "linux" }
        }
        elseif ($sourceHome -match '^[A-Za-z]:[\\/]' -or $sourceHome -match '^\\\\') {
            $sourcePlatform = "windows"
        }
    }
    if ($sourcePlatform -and $sourcePlatform -ne "windows" -and -not $DoAllowPlatformMismatch) {
        Fail "refusing $sourcePlatform bundle import into Windows; use Agent Bridge pointer-sync instead (or pass -AllowPlatformMismatch for explicit disaster recovery)"
    }
    $writers = Get-CodexWriterProcesses
    if ($writers -and -not $DoRestart) {
        Fail "close Codex before import, or pass -Restart to stop detected writer processes"
    }
    if ($DoRestart) {
        Stop-CodexDesktop
    }

    New-Item -ItemType Directory -Force -Path $CodexHomePath | Out-Null
    $backupDir = Backup-Target -CodexHomePath $CodexHomePath

    Copy-FileIfExists (Join-Path $Bundle ".codex-global-state.json") (Join-Path $CodexHomePath ".codex-global-state.json")
    Copy-FileIfExists (Join-Path $Bundle "session_index.jsonl") (Join-Path $CodexHomePath "session_index.jsonl")
    Copy-FileIfExists (Join-Path $Bundle "external_agent_session_imports.json") (Join-Path $CodexHomePath "external_agent_session_imports.json")
    Copy-FileIfExists (Join-Path $Bundle "config.toml") (Join-Path $CodexHomePath "config.toml")

    Restore-SqliteIfExists (Join-Path $Bundle "state_5.sqlite") (Join-Path $CodexHomePath "state_5.sqlite")
    Restore-SqliteIfExists (Join-Path $Bundle "sqlite\state_5.sqlite") (Join-Path $CodexHomePath "sqlite\state_5.sqlite")
    Restore-SqliteIfExists (Join-Path $Bundle "logs_2.sqlite") (Join-Path $CodexHomePath "logs_2.sqlite")
    Restore-SqliteIfExists (Join-Path $Bundle "memories_1.sqlite") (Join-Path $CodexHomePath "memories_1.sqlite")
    Restore-SqliteIfExists (Join-Path $Bundle "goals_1.sqlite") (Join-Path $CodexHomePath "goals_1.sqlite")

    Copy-DirectoryIfExists (Join-Path $Bundle "sessions") (Join-Path $CodexHomePath "sessions")
    Copy-DirectoryIfExists (Join-Path $Bundle "archived_sessions") (Join-Path $CodexHomePath "archived_sessions")
    Copy-DirectoryIfExists (Join-Path $Bundle "ambient-suggestions") (Join-Path $CodexHomePath "ambient-suggestions")
    Copy-DirectoryIfExists (Join-Path $Bundle "attachments") (Join-Path $CodexHomePath "attachments")
    Copy-DirectoryIfExists (Join-Path $Bundle "generated_images") (Join-Path $CodexHomePath "generated_images")

    Write-Host "Imported Codex sidebar/session bundle from: $Bundle"
    Write-Host "Target backup saved at: $backupDir"

    if ($DoRefresh) {
        Refresh-SidebarState -CodexHomePath $CodexHomePath
    }
    if ($DoRestart) {
        Start-CodexDesktop
    }
}

$CodexHome = Expand-LocalPath $CodexHome

switch ($Mode) {
    "export" {
        if (-not $Out) { Fail "export requires -Out DIR" }
        Export-Bundle -CodexHomePath $CodexHome -Bundle (Expand-LocalPath $Out)
    }
    "import" {
        if (-not $From) { Fail "import requires -From DIR" }
        if (-not $Yes) { Fail "import overwrites target state; pass -Yes after reviewing the bundle" }
        Import-Bundle -CodexHomePath $CodexHome -Bundle (Expand-LocalPath $From) -DoRefresh ([bool]$RefreshSidebar) -DoRestart ([bool]$Restart) -DoAllowPlatformMismatch ([bool]$AllowPlatformMismatch)
    }
}
