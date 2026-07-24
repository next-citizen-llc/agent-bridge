param(
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$BinDir = (Join-Path $HOME ".local\bin"),
    [switch]$RemoveHooks,
    [switch]$RemovePath
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path $ProjectDir).Path
$AgentCmd = Join-Path $BinDir "agent.cmd"
$StateDir = Join-Path $HOME ".local\state\agent-bridge"

$expected = @"
@echo off
set "PYTHONPATH=$ProjectDir;%PYTHONPATH%"
where py >NUL 2>NUL
if %ERRORLEVEL%==0 (
  py -3 -m agent_bridge.cli %*
) else (
  python -m agent_bridge.cli %*
)
"@

if ($RemoveHooks) {
    $env:PYTHONPATH = "$ProjectDir;$env:PYTHONPATH"
    $env:AGENT_BRIDGE_HOOK_AGENT = $AgentCmd
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m agent_bridge.cli code hooks uninstall --client all
    } else {
        & python -m agent_bridge.cli code hooks uninstall --client all
    }
    Remove-Item Env:\AGENT_BRIDGE_HOOK_AGENT -ErrorAction SilentlyContinue
}

if (-not (Test-Path $AgentCmd)) {
    Write-Host "Agent launcher already absent: $AgentCmd"
} else {
    $current = Get-Content -Raw -Path $AgentCmd
    if ($current.TrimEnd() -ne $expected.TrimEnd()) {
        throw "Preserved non-matching launcher: $AgentCmd"
    }
    Remove-Item -Path $AgentCmd
    Write-Host "Removed Agent Bridge launcher: $AgentCmd"
}

if ($RemovePath) {
    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($currentPath) {
        $parts = $currentPath -split ";" | Where-Object { $_ -and $_ -ne $BinDir }
        [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
        Write-Host "Removed $BinDir from the user PATH by explicit -RemovePath request."
    }
}

Write-Host "Retained runtime state: $StateDir"
