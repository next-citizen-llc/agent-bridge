# Codex Windows and WSL Pointer Sync

Agent Bridge keeps Windows-native Codex and Linux-native Codex stores separate.
Each runtime publishes a small project and recent-task catalog to
`SharedAgentData/AgentBridgePointerSync/v1`; neither runtime imports the other
runtime's SQLite database or global UI state.

This is the supported default for using Codex on both Windows and Ubuntu/WSL.

## Privacy and State Boundary

The publisher reads only an explicit allowlist of thread-index columns. Shared
conversation records contain a bounded title, native task ID, update time,
project ID, runtime/machine ID, status flags, native working directory, and a
resume descriptor.

The shared tree never contains:

- prompt previews or first-user-message fields;
- transcript/session JSONL, tool output, attachments, or generated files;
- Codex databases, global UI state, credentials, or configuration;
- raw Git URLs that may contain credentials, branches, commits, or hostnames.

Each publication writes an immutable generation containing the two JSONL files
and a SHA-256 manifest, then atomically advances `current.json`. Readers verify
the pointed generation and fall back to the newest prior valid generation if
OneDrive exposes the pointer before all new files arrive. The publisher retains
eight generations, so a partial cloud sync returns a stale-but-consistent source
instead of making that runtime disappear.

## Commands

Publish this runtime and inspect all currently visible sources:

```text
agent code pointer-sync publish
agent code pointer-sync status
agent code pointer-sync projects
agent code pointer-sync recent --limit 50
```

Repository discovery is privacy-safe by default: publishers use Codex project
state, configured project roots, and native thread working directories. To
explicitly include every Git repository under `~/Code`, use
`agent code pointer-sync publish --code-scan`.

`projects` groups Windows and WSL aliases by normalized network Git origin when
one exists, then by the shared project registry, then by a conservative name
fallback. It does not rewrite either runtime's native project roots.

The Codex SessionStart hook makes a fail-open publication without scanning every
repository or historical thread root. It merges with the last hash-verified full
project snapshot so startup cannot shrink the catalog. A periodic scheduler
performs the fuller authoritative scan and prunes stale project roots:

```text
agent code pointer-sync install-scheduler --interval-seconds 3600
agent code pointer-sync status
```

The scheduler is Task Scheduler on Windows, a systemd user timer on Linux/WSL,
and a LaunchAgent on macOS. Its generated command pins the resolved `agent`,
Codex home, shared-data root, and no-`~/Code` privacy policy so it does not depend
on an interactive shell. `--include-code-scan` is an explicit scheduler opt-in.

Remove only the managed publisher job with:

```text
agent code pointer-sync uninstall-scheduler
```

Set `AGENT_BRIDGE_POINTER_SYNC_DISABLED=1` to disable startup-hook publication.
The periodic job is independent and must be uninstalled separately.

## WSL Root Configuration

WSL does not infer Windows OneDrive locations. Configure its Linux-native Agent
Bridge installation once, using the mounted Windows paths that exist on the
machine:

```bash
agent code preflight roots \
  --set-skills "/mnt/c/Users/thist/OneDrive - nextcz.com/SharedAgentSkills" \
  --set-data "/mnt/c/Users/thist/OneDrive - nextcz.com/SharedAgentData" \
  --set-conversations "/mnt/c/Users/thist/OneDrive/SharedAgentConversations" \
  --json
```

Use the absolute Linux launcher when installing from a shell whose inherited
PATH may expose Windows Store shims:

```bash
AGENT_BRIDGE_HOOK_AGENT="$HOME/.local/bin/agent" \
  "$HOME/.local/bin/agent" code hooks install --client codex

AGENT_BRIDGE_HOOK_AGENT="$HOME/.local/bin/agent" \
  "$HOME/.local/bin/agent" code pointer-sync install-scheduler
```

## Windows Sidebar Recovery

If a prior full-state import inserted foreign project paths into Codex Desktop,
repair only the Windows sidebar projection. Preview and stage are safe while
Codex is running:

```text
agent code sidebar-repair preview --remap-windows
agent code sidebar-repair stage --remap-windows
agent code sidebar-repair status
```

The remap mode resolves imported macOS and WSL-rendered roots against verified
Windows folders, preferring native Git checkouts and the project registry. It
removes projects that have no Windows-native folder instead of leaving a broken
sidebar link. The older source-backup projection remains available by omitting
`--remap-windows`, but it may contain obsolete wrapper folders.

Applying is deliberately offline. Fully close Codex Desktop, then run from a
separate Windows terminal:

```text
agent code sidebar-repair apply-remap
```

`apply-remap` builds the projection from the final state flushed during Codex
shutdown and immediately applies it. A previously staged projection can instead
be applied with `apply-pending`, but its recorded live-state hash must still
match; if Codex changed the sidebar after staging, the command refuses and asks
you to restage after Codex is closed.

The apply command refuses to run while `ChatGPT.exe` from the Codex package,
`codex.exe`, or the Codex code-mode host is active. It hashes and revalidates the
staged source, backs up both `.codex-global-state.json` and its companion `.bak`
file, and changes only project/sidebar fields. Keeping those two files aligned
prevents Codex startup from restoring stale foreign paths from the companion.
Native databases, session files, projectless-task IDs, prompt history, and
unrelated Electron preferences remain untouched. If post-write validation
fails, the command atomically restores both pre-repair files from the backup.

## Full Native Bundles

The older sidebar bundle scripts are disaster-recovery tools for same-platform
native-store migration. They are not a synchronization mechanism between
Windows, WSL, and macOS. Use pointer sync for routine cross-runtime visibility.
