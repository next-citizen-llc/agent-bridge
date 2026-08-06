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
$LauncherTemplate = Join-Path $PSScriptRoot "agent.cmd.template"

$expected = (Get-Content -Raw -Path $LauncherTemplate).Replace("__AGENT_BRIDGE_PROJECT_DIR__", $ProjectDir)

$launcherPresent = Test-Path $AgentCmd
if ($launcherPresent) {
    $current = Get-Content -Raw -Path $AgentCmd
    if ($current.TrimEnd() -ne $expected.TrimEnd()) {
        throw "Preserved non-matching launcher: $AgentCmd"
    }
}

if ($RemoveHooks) {
    $env:PYTHONPATH = "$ProjectDir;$env:PYTHONPATH"
    $env:AGENT_BRIDGE_HOOK_AGENT = $AgentCmd
    if ($launcherPresent) {
        & $AgentCmd code hooks uninstall --client all
    } elseif ($env:AGENT_BRIDGE_PYTHON) {
        & $env:AGENT_BRIDGE_PYTHON -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        if ($LASTEXITCODE -ne 0) {
            throw "AGENT_BRIDGE_PYTHON is not a usable Python 3.11+ interpreter."
        }
        & $env:AGENT_BRIDGE_PYTHON -m agent_bridge.cli code hooks uninstall --client all
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        if ($LASTEXITCODE -eq 0) {
            & py -3 -m agent_bridge.cli code hooks uninstall --client all
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -ne 0) {
                throw "Python 3.11 or newer is required to remove Agent Bridge hooks."
            }
            & python -m agent_bridge.cli code hooks uninstall --client all
        } else {
            throw "Python 3.11 or newer is required to remove Agent Bridge hooks."
        }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        if ($LASTEXITCODE -ne 0) {
            throw "Python 3.11 or newer is required to remove Agent Bridge hooks."
        }
        & python -m agent_bridge.cli code hooks uninstall --client all
    } else {
        throw "Python 3.11 or newer is required to remove Agent Bridge hooks."
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Agent Bridge hook removal failed with exit code $LASTEXITCODE."
    }
    Remove-Item Env:\AGENT_BRIDGE_HOOK_AGENT -ErrorAction SilentlyContinue
}

if (-not $launcherPresent) {
    Write-Host "Agent launcher already absent: $AgentCmd"
} else {
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
