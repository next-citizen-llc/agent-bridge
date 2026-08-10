# Legacy Full Codex Sidebar Bundle

> Warning: this is a destructive, full native-store migration tool. Do not use
> it to synchronize Windows, WSL/Linux, and macOS. Absolute workspace paths and
> native UI/database assumptions are not portable across those runtimes. Use
> [`codex-pointer-sync.md`](codex-pointer-sync.md) for routine cross-runtime
> project and recent-task visibility.

This runbook copies the Codex Desktop state that controls visible sessions and
workspace groupings in the sidebar from one machine to another.

Use it only for explicit same-platform disaster recovery when the target should
be replaced by the source native store. It is not an always-on sync layer.
New exports record their platform, and imports refuse a platform mismatch by
default. The override exists only for explicit disaster recovery and does not
make foreign absolute paths portable.

Imports also refuse to run while Codex writer processes are active. Close Codex
first, or use the same-platform restart option from a separate terminal so the
script can stop and re-check those writers before replacing native state.

## What Gets Synced

The helper copies the state surfaces that affect sidebar sessions and workspace
grouping:

- `~/.codex/state_5.sqlite`
- `~/.codex/sqlite/state_5.sqlite`
- `~/.codex/.codex-global-state.json`
- `~/.codex/session_index.jsonl`
- `~/.codex/external_agent_session_imports.json`
- `~/.codex/config.toml`
- `~/.codex/sessions/`
- `~/.codex/archived_sessions/`
- `~/.codex/ambient-suggestions/`
- `~/.codex/attachments/`
- `~/.codex/generated_images/`

`state_5.sqlite` contains the `threads.cwd` values that drive sidebar grouping.
`.codex-global-state.json` contains workspace labels and ordering, including
`electron-workspace-root-labels`, `project-order`, saved workspace roots, and
thread workspace hints.

The helper does not copy plugin caches, credentials, MCP installs, shell
snapshots, or generated runtime folders.

Review `config.toml` before sharing a bundle outside your own trusted machines.
It is included because workspace migration can depend on it, but local MCP paths
or machine-specific settings may need adjustment on the target.

## Assumptions

- The target machine can access the same workspace paths, or equivalent folders
  have already been created at the same absolute paths.
- The operator accepts that import makes the target Codex sidebar/session state
  match the source bundle. Target state is backed up first, but then overwritten.
- Codex Desktop should be restarted after import. The app keeps sidebar state in
  memory while running.

## Source Machine: Export

Pick a synced location both machines can read, for example a shared OneDrive
folder:

macOS/Linux:

```bash
SYNC_ROOT="$HOME/Library/CloudStorage/OneDrive-nextcz.com/SharedAgentData/CodexSidebarSync"
BUNDLE="$SYNC_ROOT/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)"

cd "$HOME/Code/agent-bridge"
scripts/codex-sidebar-sync.sh export --out "$BUNDLE"
```

Windows PowerShell:

```powershell
$SyncRoot = Join-Path $env:OneDrive "SharedAgentData\CodexSidebarSync"
$Bundle = Join-Path $SyncRoot ("$env:COMPUTERNAME-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ"))

cd "$HOME\Code\agent-bridge"
.\scripts\codex-sidebar-sync.ps1 export -Out $Bundle
```

The export uses SQLite `.backup` for live databases rather than raw-copying
WAL-backed files.

## Target Machine: Import, Refresh, Restart

After the bundle is visible on the target machine:

macOS target:

```bash
BUNDLE="/path/to/CodexSidebarSync/<source-host>-<timestamp>"

cd "$HOME/Code/agent-bridge"
scripts/codex-sidebar-sync.sh import \
  --from "$BUNDLE" \
  --yes \
  --refresh-sidebar \
  --restart
```

Windows target:

```powershell
$Bundle = "$env:OneDrive\SharedAgentData\CodexSidebarSync\<source-host>-<timestamp>"

cd "$HOME\Code\agent-bridge"
.\scripts\codex-sidebar-sync.ps1 import `
  -From $Bundle `
  -Yes `
  -RefreshSidebar `
  -Restart
```

`--refresh-sidebar` validates `state_5.sqlite` with `pragma integrity_check` and
writes a refresh marker under `~/.codex/backups/sidebar-state-sync-refresh/`.

`--restart` quits `Codex.app` on macOS before import, then reopens it after the
copied state is in place. On Windows, `-Restart` stops a running `Codex` process
before import and starts `Codex.exe` afterward when it can resolve the install
path; if it cannot, start Codex Desktop manually after the import.

## Cross-Platform Use

Do not import these bundles across operating systems. Keep each native Codex
store authoritative for its runtime and exchange only pointer metadata through
`agent code pointer-sync`.

## Target Backup and Rollback

Every import writes a target backup before replacing files:

```text
~/.codex/backups/sidebar-state-sync-<timestamp>/
```

To roll back, import that backup bundle:

```bash
cd "$HOME/Code/agent-bridge"
scripts/codex-sidebar-sync.sh import \
  --from "$HOME/.codex/backups/sidebar-state-sync-<timestamp>" \
  --yes \
  --refresh-sidebar \
  --restart
```

## Verification

After restart, verify the target sidebar:

1. The expected workspace labels appear.
2. Recent source-machine sessions are present under the same workspaces.
3. No old duplicate workspace roots reappeared.

For a CLI check:

```bash
sqlite3 "$HOME/.codex/state_5.sqlite" \
  "select cwd, count(*) from threads group by cwd order by count(*) desc limit 20;"

jq '."electron-workspace-root-labels"' "$HOME/.codex/.codex-global-state.json"
```

If labels or projects still look stale, fully quit Codex Desktop, confirm no
Codex process remains, and reopen it:

```bash
osascript -e 'tell application "Codex" to quit'
sleep 2
pgrep -fl Codex || true
open -a Codex
```
