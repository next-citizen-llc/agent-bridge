# Codex Session and Project State Sync

Use the incremental state sync for normal Mac/Windows continuity. It publishes
one initial baseline into OneDrive, then only changed compressed chunks and
small metadata indexes. Imports merge sessions and sidebar/project structure;
they do not replace target-only state.

## What is synchronized

- live and archived Codex session JSONL;
- attachments and generated images;
- thread titles, timestamps, archive/pin state, rollout paths, and project
  working directories from `state_5.sqlite`;
- local project definitions, thread-project assignments, sidebar order,
  workspace hints, pinned threads, and projectless threads.

The sync excludes Codex log databases, config, credentials, caches, plugins,
MCP state, and the native SQLite database file. OneDrive carries the private
state archive; GitHub carries the Agent Bridge code.

## Trust and privacy boundary

This archive copies included session JSONL, raw prompts and tool content,
attachments, and generated images as-is. It is not encrypted and manifests are
not cryptographically source-authenticated; machine IDs are routing labels, not
proof of source identity. Use only a trusted, private `SharedAgentData` root
and do not apply manifests from an untrusted source. Excluding Codex config or
credential stores is a path-selection boundary only: it does not scan included
session content for secrets or redact history.

## First baseline

On either machine:

```bash
cd "$HOME/Code/agent-bridge"
agent code state-sync publish
agent code state-sync status
```

PowerShell uses the same `agent` command after Agent Bridge is installed:

```powershell
cd "$HOME\Code\agent-bridge"
agent code state-sync publish
agent code state-sync status
```

The default shared location is the configured `SharedAgentData` root. Pass
`--shared-root <SharedAgentData-path>` only when root discovery is not already
configured identically on both machines.

The first publication reads the full retained corpus. Wait for OneDrive to make
the manifest and referenced objects available on the other machine before the
first import.

## Preview and import

Preview is read-only and may run while Codex is open:

```bash
agent code state-sync apply --dry-run
```

For the actual merge, fully quit Codex Desktop, then run:

```bash
agent code state-sync apply --yes
```

Select one source when needed:

```bash
agent code state-sync apply --from-machine <machine-id> --yes
```

Override an unresolved project-root mapping explicitly:

```bash
agent code state-sync apply \
  --path-map '/Users/name/Code/project=C:\Users\name\Code\project' \
  --yes
```

The importer creates a native metadata backup before modifying indexes. If
session histories diverge, it keeps the target copy and stages the remote copy
under `~/.codex/session-sync-conflicts/` for review.

## Ongoing delta sync

Install an hourly publisher on each machine:

```bash
agent code state-sync install-scheduler
```

To make it bidirectional, explicitly enable pull on each machine:

```bash
agent code state-sync install-scheduler --pull
```

The pull job never changes native state while Codex Desktop is running. It
writes a pending marker and retries on a later scheduled run after Codex is
closed. Check with:

```bash
agent code state-sync status
```

Remove only the exact Agent Bridge scheduler with:

```bash
agent code state-sync uninstall-scheduler
```

## Retention

There is no automatic deletion, expiration, or garbage collection. Immutable
objects and disappeared-source catalog records remain retained. Do not manually
remove `AgentBridgeStateSync/v1` objects unless the loss and recovery impact has
been reviewed explicitly.

## Legacy full replacement bundle

The older scripts remain available only as a manual disaster-recovery path:

- `scripts/codex-sidebar-sync.sh`
- `scripts/codex-sidebar-sync.ps1`

They produce timestamped full copies and replace target-native state after a
backup. They also include surfaces that recurring sync does not need. Do not use
them for hourly or routine synchronization.

The legacy full export command and rollback procedure remain available through
each script's `--help` / PowerShell help. Preserve any existing bundle until the
incremental baseline and cross-machine import have been verified.
