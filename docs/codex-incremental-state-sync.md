# Codex Incremental State Sync

Use `agent code state-sync` for normal Mac and Windows conversation continuity. The first publish
creates a retained baseline in the private `SharedAgentData/AgentBridgeStateSync/v1` archive;
later publishes reuse unchanged compressed chunks and add only changed data. Imports merge sessions,
thread metadata, and project/sidebar structure while preserving target-only state.

## Data and trust boundary

The archive can contain live and archived session JSONL, raw prompts and tool output, attachments,
generated images, thread metadata, and project/sidebar state. It excludes Codex credentials,
configuration, caches, plugins, MCP state, and native database files.

The archive is not encrypted and manifests are hash-checked for damage, not cryptographically
authenticated. Only publish to and import from a trusted private `SharedAgentData` root.

## Commands

Publish this machine and inspect all available sources:

```text
agent code state-sync publish
agent code state-sync status
```

Preview a selected source while Codex is open:

```text
agent code state-sync apply --from-machine <machine-id> --dry-run
```

The preview reports project roots that lack a target-platform registry mapping. Resolve them in the
shared project registry or pass explicit mappings before applying:

```text
agent code state-sync apply \
  --from-machine <machine-id> \
  --path-map '/source/project=C:\target\project' \
  --dry-run
```

For the actual merge, fully close Codex Desktop and run from a separate terminal:

```text
agent code state-sync apply --from-machine <machine-id> --yes
```

The importer backs up `state_5.sqlite`, `.codex-global-state.json`, and `session_index.jsonl` under
`~/.codex/backups/state-sync-<timestamp>-<suffix>/` before changing native indexes. Conflicting
remote session artifacts are placed under `~/.codex/session-sync-conflicts/`; target-only and newer
target state are retained.

## Ongoing synchronization

Install an hourly publisher:

```text
agent code state-sync install-scheduler
```

Explicitly enable additive pulls when Codex is closed:

```text
agent code state-sync install-scheduler --pull --from-machine <machine-id> --project-registry <projects.json>
```

Before any native-state mutation, the pull scheduler probes twice for Codex Desktop. A failed probe
is treated as running, so the pull writes a pending marker and retries on a later scheduled run.
An explicit project registry is pinned into the scheduled command, which is useful when more than
one shared conversations root exists. Pulls also refuse to register mapped project directories that
do not exist. Remove only the managed task with:

```text
agent code state-sync uninstall-scheduler
```

The archive is append-only and has no automatic garbage collection. Do not remove retained objects
without reviewing recovery impact.

## Legacy bundle distinction

The scripts documented in [Legacy Full Codex Sidebar Bundle](codex-sidebar-state-sync.md) replace a
target native store and are reserved for explicit same-platform disaster recovery. Pointer sync is
metadata-only. Incremental state sync is the additive session continuity mechanism.
