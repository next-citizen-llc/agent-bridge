param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$BinDir = (Join-Path $HOME ".local\bin"),
    [switch]$InstallHooks,
    [switch]$UpdatePath,
    [switch]$SkipPathUpdate,
    [switch]$RegisterMcp,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path $ProjectDir).Path
$StateDir = Join-Path $HOME ".local\state\agent-bridge"
$AgentCmd = Join-Path $BinDir "agent.cmd"

New-Item -ItemType Directory -Force -Path $BinDir, $StateDir | Out-Null

$cmd = @"
@echo off
set "PYTHONPATH=$ProjectDir;%PYTHONPATH%"
where py >NUL 2>NUL
if %ERRORLEVEL%==0 (
  py -3 -m agent_bridge.cli %*
) else (
  python -m agent_bridge.cli %*
)
"@

$launcherStatus = "installed"
if (Test-Path $AgentCmd) {
    $current = Get-Content -Raw -Path $AgentCmd
    if ($current.TrimEnd() -eq $cmd.TrimEnd()) {
        $launcherStatus = "already installed"
    } elseif (-not $Force) {
        throw "Refusing to replace existing launcher: $AgentCmd. Inspect it, choose another -BinDir, or rerun with -Force."
    } else {
        Set-Content -Path $AgentCmd -Value $cmd -Encoding ASCII
        $launcherStatus = "replaced by explicit -Force"
    }
} else {
    Set-Content -Path $AgentCmd -Value $cmd -Encoding ASCII
}

if ($UpdatePath -and -not $SkipPathUpdate) {
    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($currentPath) {
        $parts = $currentPath -split ";" | Where-Object { $_ }
    }
    if ($parts -notcontains $BinDir) {
        $newPath = if ($currentPath) { "$currentPath;$BinDir" } else { $BinDir }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        $env:Path = "$env:Path;$BinDir"
        Write-Host "Added $BinDir to the user PATH by explicit -UpdatePath request."
    }
}

if ($InstallHooks) {
    $env:AGENT_BRIDGE_HOOK_AGENT = $AgentCmd
    & $AgentCmd code hooks install --client all
    Remove-Item Env:\AGENT_BRIDGE_HOOK_AGENT -ErrorAction SilentlyContinue
    $hookStatus = "installed by explicit request"
} else {
    $hookStatus = "not installed; opt in with: agent code hooks install --client all"
}

if ($RegisterMcp) {
    $MailboxMcp = Join-Path $ProjectDir "agent_bridge\mailbox_mcp.py"
    $PythonMcpCommand = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $PythonMcpCommand = @("py", "-3")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $PythonMcpCommand = @("python")
    }

    if ($PythonMcpCommand.Count -eq 0) {
        Write-Warning "No Python command found on PATH; skipped MCP registration."
    }

    if (Get-Command claude -ErrorAction SilentlyContinue) {
        if ($PythonMcpCommand.Count -gt 0) {
            & claude mcp add --scope user mailbox -- @PythonMcpCommand $MailboxMcp
        }
    } else {
        Write-Warning "Claude CLI not found on PATH; skipped Claude MCP registration."
    }
    if (Get-Command codex -ErrorAction SilentlyContinue) {
        if ($PythonMcpCommand.Count -gt 0) {
            & codex mcp add mailbox -- @PythonMcpCommand $MailboxMcp
        }
    } else {
        Write-Warning "Codex CLI not found on PATH; skipped Codex MCP registration."
    }
}

Write-Host "Agent launcher: $AgentCmd ($launcherStatus)"
Write-Host "State directory: $StateDir"
Write-Host "Startup hooks: $hookStatus"
Write-Host "For this shell, if needed: `$env:Path = `"$BinDir;`$env:Path`""
Write-Host "Persist PATH only by request: .\scripts\install.ps1 -UpdatePath"
Write-Host "Uninstall: .\scripts\uninstall.ps1"
