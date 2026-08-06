# Incremental Codex State Sync

## Status

Implemented in `agent_bridge/state_sync.py` and exposed through
`agent code state-sync`.

The recurring sync is intentionally split across two existing systems:

- GitHub distributes the Agent Bridge implementation to each machine.
- OneDrive `SharedAgentData` carries private Codex session objects and normalized
  project/thread indexes.

GitHub does not carry transcripts. A first machine publication is still a
baseline-sized operation; later publications are append-aware deltas.

## Why the old payload was large

The legacy sidebar exporter copied a complete native snapshot on every run. A
representative Mac export contained session JSONL, archived sessions, the Codex
log database, the thread-state database, and the Electron global-state file.
The log database and repeated copies of unchanged transcripts added substantial
bytes without helping a recurring merge.

The incremental format publishes only:

- `sessions/` and `archived_sessions/` JSONL files;
- `attachments/` and `generated_images/`;
- normalized rows from `state_5.sqlite`'s `threads` table;
- normalized local projects and the sidebar assignment/order fragment from
  `.codex-global-state.json`.

It does not publish `logs_*.sqlite`, config, credentials, cookies, caches, MCP
configuration, or the native SQLite database itself. This is path selection,
not content-level secret scanning: included session JSONL, attachments, and
generated images are copied as-is and are not redacted.

## Shared layout

```text
SharedAgentData/
  AgentBridgeStateSync/
    v1/
      objects/
        sha256/<prefix>/<chunk-sha256>.gz
      machines/
        <machine-id>/codex/
          manifest.json
          artifact-index.jsonl
          thread-index.jsonl
          project-index.jsonl
          ui-state.json
          events.jsonl
```

Objects are immutable, deterministic gzip files addressed by the SHA-256 of a
4 MiB uncompressed chunk. Metadata files are atomically replaced and their
hashes are recorded in the manifest, which is written last. An importer rejects
a mixed or incomplete metadata generation.

## Delta behavior

Codex session JSONL is append-only in normal operation. For a changed session,
the publisher verifies the final stable 4 MiB block, reuses every complete
prefix chunk, and reads from the first changed/partial chunk onward. Therefore:

- an unchanged file uploads zero objects;
- appending a small turn normally uploads one replacement tail chunk;
- identical chunks across sessions or machines are stored once;
- a same-size rewrite is fully re-read rather than treated as an append;
- non-session artifacts are fully re-read when changed, while their identical
  chunks still deduplicate.

The first baseline has to read and encode the existing corpus once. Delta sync
eliminates repeated transfer of the unchanged buildup; it does not make future
conversation growth disappear.

## Retention and conflict rules

The archive has no automatic deletion or time-based expiration.

- A prior catalog entry whose native source disappears is retained with
  `source_present: false`; it is not automatically re-imported.
- Import is additive. Target-only sessions and projects remain in place.
- A remote session that byte-prefix-extends the target safely extends it after
  preserving the prior target file in the import backup.
- A target that already contains the longer prefix is kept.
- Divergent copies are never overwritten. The remote copy is reconstructed
  under `~/.codex/session-sync-conflicts/<machine>/<artifact-id>/...`.
- Before changing native indexes, import backs up `state_5.sqlite`,
  `.codex-global-state.json`, and `session_index.jsonl` under
  `~/.codex/backups/state-sync-<timestamp>/`.

There is deliberately no garbage collector. Storage growth remains visible and
operator-controlled.

## Project and sidebar merge

The publisher normalizes both current Codex project schemas:

- current `local-projects` plus ID-based `project-order`;
- legacy path-based `project-order`.

The importer maps source projects in this order:

1. an explicit `--path-map SOURCE=TARGET`;
2. the cross-harness project registry in
   `SharedAgentConversations/projects/_registry/projects.json`;
3. an already matching target project by logical ID, Git remote, or name;
4. a deterministic expected path under the target user's `Code` directory,
   with a warning.

Target-local project IDs are retained. New IDs are deterministic. Thread `cwd`
values preserve subdirectory suffixes, and project assignments, sidebar order,
workspace hints, pinned threads, and projectless threads are merged rather than
replaced.

## Safety boundary

Publication can run while Codex Desktop is active. A settle window defers a
file that is currently changing. Import requires Codex Desktop to be closed;
`--defer-if-running` records a pending marker instead of modifying live state.

Session files contain private prompt and tool history, and included attachments
and generated images are copied as-is. Use only a trusted, private
`SharedAgentData` location shared by the intended machines. The archive is not
encrypted or cryptographically source-authenticated: metadata hashes detect
mixed or damaged generations but do not prove who authored a manifest, and
machine IDs are labels rather than identities. Never apply an untrusted
manifest.

## Commands

```bash
# Baseline once; later invocations publish only changed chunks and metadata.
agent code state-sync publish

# Inspect machines, byte counts, scheduler state, and pending import.
agent code state-sync status

# Review, then additively merge remote machines while Codex is closed.
agent code state-sync apply --dry-run
agent code state-sync apply --yes

# Hourly publication only.
agent code state-sync install-scheduler

# Hourly publication plus pull when Codex is closed; otherwise defer safely.
agent code state-sync install-scheduler --pull
```

macOS uses
`~/Library/LaunchAgents/com.nextcz.agent-bridge.state-sync.plist`. Windows uses
the Task Scheduler entry `Agent Bridge State Sync`. Both call the same one-shot
CLI; no daemon owns a separate sync implementation.

## Known boundary

This synchronizes Codex native sessions and project/sidebar structure between
Codex installations. It does not make Claude and Codex native chat histories
interchangeable. Portable cross-harness continuation still uses the checkpoint
registry and project files.
