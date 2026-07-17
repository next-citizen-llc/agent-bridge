# Always-On State Sync Evaluation

## Summary

Agent Bridge should grow a lightweight state-sync worker that writes current
harness presence, conversation identifiers, workspace mappings, and freshness
events into `SharedAgentData`. The worker should remain idempotent and callable
as a one-shot CLI. Hooks, periodic in-harness calls, OS schedulers, and optional
desktop app/service packaging should all call the same worker rather than
owning separate sync logic.

The right target is not raw transcript mirroring. The reliable portable layer is
a bounded index of native state plus pointers to local source files and explicit
full-bundle exports when a machine-to-machine sidebar import is intended.

## Goals

- On harness load or resume, publish a fresh machine/harness record and light
  conversation/workspace index.
- On harness exit, publish a best-effort final snapshot where an exit hook is
  available.
- During active bridge/workflow/loop runs, periodically emit sync events without
  blocking the run.
- When no harness is running, keep state fresh through macOS LaunchAgent and
  Windows Task Scheduler jobs.
- Keep the implementation useful without a daemon, while leaving a clean path
  for a packaged menu bar/tray app or service.

## Non-Goals

- Do not claim product-native Claude and Codex chat histories are interchangeable.
- Do not copy raw caches, credentials, cookies, private browser profiles, MCP
  auth stores, or bulky transcripts into `SharedAgentData`.
- Do not auto-import another machine's native SQLite/sidebar state without an
  explicit operator action.
- Do not make existing `agent code bridge`, `loop`, `workflow`, or mailbox flows
  require a daemon.

## Proposed Shared Layout

Use a new short-path tree:

```text
SharedAgentData/
  AgentBridgeStateSync/
    v1/
      manifest.json
      machines/
        <machine-id>/
          machine.json
          harnesses/
            codex/
              heartbeat.json
              conversation-index.jsonl
              workspace-index.jsonl
              events.jsonl
            claude/
              heartbeat.json
              conversation-index.jsonl
              workspace-index.jsonl
              events.jsonl
      aggregate/
        harnesses.jsonl
        conversations.jsonl
        workspaces.jsonl
        freshness.json
      bundles/
        codex-sidebar/
          <machine>-<timestamp>/
```

Write per-machine files first. Aggregate files are generated views and can be
rebuilt. This prevents one stale machine from overwriting another machine's
facts and keeps OneDrive conflict recovery straightforward.

## Light Snapshot Schema

Each snapshot should include:

- `schema_version`
- `machine_id`, `hostname`, `platform`, `username_hash`
- `client`: `codex`, `claude`, or `other`
- `reason`: `session_start`, `session_resume`, `session_exit`,
  `periodic_active`, `periodic_idle`, `manual`
- `created_at_utc`
- `workspace_roots`: normalized path, label, git root, source
- `conversation_refs`: native id, title/name when available, workspace path,
  updated time, pinned/archived flags when available, source file path, content
  hash of the source record when cheap
- `state_sources`: checked files and mtimes
- `warnings`: missing files, stale cloud folder, unreadable DB, lock timeout

The light snapshot should be bounded: no prompt bodies, no tool outputs, no
attachments, and no raw transcripts. Full native sync remains an explicit
bundle export using the sidebar/session scripts.

## Event Model

Use append-only JSONL with stable event ids and dedupe keys:

```json
{
  "event_id": "evt_...",
  "dedupe_key": "machine/client/reason/timestamp-bucket",
  "created_at_utc": "2026-07-05T20:00:00Z",
  "machine_id": "tts_Tristans-MacBook-Pro_local",
  "client": "codex",
  "reason": "periodic_idle",
  "workspace_count": 25,
  "conversation_count": 418,
  "status": "ok"
}
```

For frequently updated files, write `*.tmp` and atomically replace. For JSONL,
append locally and tolerate duplicate events by dedupe key in aggregate views.

## Hook Layer

Current Agent Bridge already installs `SessionStart` hooks for Codex and Claude
and writes the shared `Agent-Bridge/registry` heartbeat. That should evolve into:

```bash
agent code state-sync snapshot --client codex --reason session_start --light
agent code state-sync snapshot --client claude --reason session_start --light
```

Do not assume every harness has a reliable exit hook. Where a native
`SessionEnd` or process-exit hook exists, install it. Where it does not, rely on
the active periodic and idle scheduler layers for at-least-once exit-adjacent
freshness.

## Active Periodic Layer

Bridge-owned long-running commands should schedule non-blocking snapshots:

- on run start
- after material task state changes
- every 10 to 15 minutes during long runs
- on run completion or failure

Implementation should be best effort: if a snapshot fails, write a trace warning
and keep the primary bridge/loop/workflow command moving. The sync worker must
use bounded timeouts and avoid waiting on OneDrive uploads.

## Idle Scheduler Layer

Install OS-level one-shot scheduler jobs that call the same CLI:

macOS:

```text
~/Library/LaunchAgents/com.nextcz.agent-bridge.state-sync.plist
```

Suggested behavior:

- `StartInterval`: 3600 seconds
- `RunAtLoad`: true
- command: `agent code state-sync snapshot --client all --reason periodic_idle --light`
- logs: `~/.local/state/agent-bridge/state-sync/logs/`

Windows:

```text
Task Scheduler: Agent Bridge State Sync
```

Suggested behavior:

- trigger at logon
- repeat every 1 hour indefinitely
- command: `%USERPROFILE%\.local\bin\agent.cmd code state-sync snapshot --client all --reason periodic_idle --light`
- logs: `%USERPROFILE%\.local\state\agent-bridge\state-sync\logs\`

The scheduler should never auto-import state. It only publishes the current
machine's light snapshot and updates aggregate views.

## Full Bundle Sync

The existing Codex sidebar/session scripts are the right tool for explicit full
native state transfer:

- `scripts/codex-sidebar-sync.sh`
- `scripts/codex-sidebar-sync.ps1`

These should remain manual or operator-confirmed because they overwrite target
native state after making a backup. The light state-sync worker can advertise
that a newer full bundle exists, but importing that bundle should stay explicit.

## Packaging Options

### Option A: CLI plus OS scheduled jobs

This is the best MVP.

Pros:

- smallest change from current architecture
- no signing, installer, tray, or service complexity
- debuggable with ordinary logs and JSON files
- works even when a daemon crashes because every invocation is one-shot

Cons:

- less visible to the user
- no live tray status or pause button
- scheduler installation needs per-OS care

### Option B: Menu bar/tray app wrapper

Build a small "Agent Bridge Sync" desktop wrapper around the CLI.

Core UI:

- last sync time by harness
- OneDrive/shared folder health
- "Sync now"
- "Export full Codex bundle"
- "Pause for 1 hour"
- "Reveal logs"

Implementation choices:

- Python + PyInstaller is fastest because Agent Bridge is already Python.
- Tauri is cleaner for a polished cross-platform UI but adds a Node/Rust stack.
- A pure native app is overkill until the sync schema is stable.

Pros:

- good user visibility
- easier to recover from OneDrive/offline/auth/path failures
- can install/update scheduler jobs

Cons:

- signing/notarization on macOS and SmartScreen/trust friction on Windows
- more packaging surface than the sync worker itself
- risk of drifting into a second control plane if it owns logic

### Option C: Long-running daemon/service

Use this only after Option A proves insufficient.

macOS:

- LaunchAgent for user context is preferable to LaunchDaemon because it sees
  the user's OneDrive and harness state.
- A daemon should still call the same library functions as the CLI.

Windows:

- Start with Task Scheduler.
- Move to a Windows Service only if we need file watching, push notifications,
  or strict uptime. A service may not see the same user OneDrive context unless
  configured carefully.

Pros:

- near-real-time freshness
- can watch files instead of polling
- can expose richer health status

Cons:

- more brittle around user session, OneDrive availability, and permissions
- harder to debug remotely
- creates exactly the always-on dependency the current bridge design avoids

## Recommendation

Build Option A first, with the code shaped so Option B can wrap it.

1. Add `agent_bridge/state_sync.py` with pure functions for resolving shared
   roots, reading light Codex/Claude indexes, writing snapshots, and rebuilding
   aggregate views.
2. Add CLI:

   ```bash
   agent code state-sync snapshot --client all --reason manual --light
   agent code state-sync status --json
   agent code state-sync install-scheduler --platform auto
   agent code state-sync uninstall-scheduler --platform auto
   ```

3. Extend the current `SessionStart` hook to call the state-sync worker after
   the existing heartbeat.
4. Add best-effort active snapshots in bridge/loop/workflow run lifecycle.
5. Add macOS LaunchAgent and Windows Task Scheduler installers.
6. Only then build the optional menu bar/tray app as a UI shell over the same
   commands.

## Risk Controls

- Use SQLite backup APIs or read-only connections for live Codex state.
- Keep full bundles out of hourly sync; hourly snapshots should be kilobytes or
  low megabytes, not gigabytes.
- Keep aggregate writes rebuildable and per-machine source files authoritative.
- Include `schema_version` and `generated_by` in every file.
- Use atomic replace for JSON files and append-only JSONL for event streams.
- Respect cloud file availability: fail with a warning rather than blocking the
  harness when OneDrive is offline or a file is online-only.
- Redact or hash usernames/account identifiers when they are not needed for
  routing.
- Add tests with temporary fake Codex/Claude homes and fake shared roots before
  installing any scheduler.

## Near-Term Task Breakdown

1. Define `AgentBridgeStateSync/v1` schema fixtures and tests.
2. Implement Codex light reader:
   `state_5.sqlite`, `session_index.jsonl`, `.codex-global-state.json`.
3. Implement Claude light reader:
   `.claude/projects/<encoded-cwd>/`, `.claude/sessions/*.json`, and task ids
   when present.
4. Implement snapshot writer plus aggregate rebuild.
5. Add CLI and docs.
6. Wire existing SessionStart hooks to state-sync.
7. Add active lifecycle sync points to bridge/loop/workflow.
8. Add `install-scheduler` for macOS LaunchAgent and Windows Task Scheduler.
9. Add status/doctor checks for scheduler installation and last successful sync.
10. Re-evaluate the tray/menu app once the CLI and scheduler have run for a
    week without corrupting or bloating the shared folder.
