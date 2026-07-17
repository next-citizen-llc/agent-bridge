# Session Context and Readiness

Agent Bridge separates four facts that are easy to conflate:

1. A harness process started and registered itself.
2. Its local command and configuration are installed.
3. Its model account and required sources are authenticated.
4. Its project context matches the canonical context source.

A registry heartbeat proves only the first fact. Use work preflight and context checks for the
others.

## Shared roots

The three shared stores may live in different sync accounts. Configure them explicitly once per
machine:

```bash
agent code preflight roots \
  --set-skills /path/to/SharedAgentSkills \
  --set-data /path/to/SharedAgentData \
  --set-conversations /path/to/SharedAgentConversations
```

The mapping is stored in `~/.config/agent-bridge/roots.json`. `AGENT_BRIDGE_SHARED_SKILLS_ROOT`,
`AGENT_BRIDGE_SHARED_DATA_ROOT`, and `AGENT_BRIDGE_SHARED_CONVERSATIONS_ROOT` override it. Run
`agent code preflight roots --json` to expose duplicate candidates instead of silently choosing
between sync trees.

## Startup registration

`agent code hooks status --client all --json` reports every known client and surface from
`agent_bridge/surfaces.json`, including installation and shared-registration freshness. Agents
added to `agents.json` without a surface declaration appear as `unsupported` instead of being
silently omitted. Native SessionStart integrations are used for Codex CLI/GUI, Claude
Code CLI, and Grok CLI. Where no verified native lifecycle exists, installation creates a clearly
named wrapper:

- `claude-gui-bridge` registers the GUI surface and opens Claude when explicitly enabled.
- `grok-gui-bridge` registers the GUI surface and opens Grok in Microsoft Edge.
- `agy-bridge` registers the CLI surface and then executes AGY.
- `ollama-bridge` registers the local API surface and then executes Ollama.

Install active hooks and wrappers with:

```bash
agent code hooks install --client all
```

The Unix installer does this by default; set `AGENT_BRIDGE_SKIP_HOOKS=1` to install only the
global command.

Claude is declared but skipped by that command because it is inactive by default. Install it
explicitly with `agent code hooks install --client claude`, or pass `--include-inactive`.

Surfaces without a verified native hook or safe wrapper remain `unsupported`; the status command
does not claim they are registered. Startup hooks are local, bounded, idempotent, and never run
OAuth, network probes, or agent turns.

## Readiness

Session preflight is cache-first and safe for startup:

```bash
agent code preflight session --client codex --surface gui
agent code preflight status --client codex --surface gui
```

Work preflight runs bounded live probes for the model client, configured MCP sources, GitHub, and
Ollama when selected. Configure a machine-wide required GitHub identity once:

```bash
agent code preflight configure --github-login YOUR_LOGIN --require-github
```

Then run:

```bash
agent code preflight work --client grok --surface bridge \
  --expected-github-login YOUR_LOGIN
agent code preflight work --client ollama --surface local-api \
  --expected-github-login YOUR_LOGIN
```

Bridge and loop dispatches use a fresh cached work report or run a bounded refresh. A blocked
required check stops code dispatch. Degraded review dispatches continue with a warning recorded
in the trace and task ledger. `--require-ready` makes advisory degradation blocking;
`--refresh-readiness` bypasses the cache; `--preflight-timeout` bounds each probe; and
`--no-preflight` is an explicit, traced operator override. Stale reports are never treated as
ready.

GUI auth and GUI connector health are reported as `unknown` unless that exact surface exposes a
non-interactive probe. CLI auth is never inherited by a GUI row. Grok GUI repair opens the
authenticated Microsoft Edge surface; probes never attempt login automatically. Ollama readiness
accepts loopback endpoints only.

Local reports live under `~/.local/state/agent-bridge/readiness/`. They can include local paths and
diagnostic detail. Shared publication uses an allowlisted summary that excludes paths, command
output, tokens, headers, URLs, and identities:

```bash
agent code preflight publish --client grok --surface bridge
agent code preflight flush
agent code preflight aggregate --write
```

Published summaries live under `SharedAgentData/Agent-Bridge/readiness/`. The data root must be
explicitly configured, preventing publication to a discovered wrong tenant. Writes are atomic and
content-stable; changes append a deduplicated redacted event. An unavailable sync root queues the
summary locally for `flush`, while `aggregate` rebuilds the multi-machine view from authoritative
per-surface rows. The full diagnostic report always remains local. The packaged contract is
`agent_bridge/readiness_schema.json`.

## Context adapters

Context content remains in a private canonical source chosen by the operator. Agent Bridge only
renders and verifies harness-native adapters. A manifest selects modules and output files:

```json
{
  "schema_version": "1.0",
  "modules": [
    {"id": "core-policy", "path": "modules/core-policy.md"},
    {"id": "source-map", "path": "modules/source-map.md"}
  ],
  "precedence": ["canonical_modules", "harness_adapter", "manual_outside_generated_region"],
  "foreign_sources": ["legacy/GROK.md"],
  "adapters": [
    {"client": "codex", "path": "generated/AGENTS.md", "modules": ["core-policy", "source-map"]},
    {"client": "claude", "path": "generated/CLAUDE.md", "modules": ["core-policy", "source-map"]},
    {"client": "grok", "path": "generated/GROK.md", "modules": ["core-policy", "source-map"]}
  ]
}
```

Generate or verify adapters with:

```bash
agent code context install --manifest /private/context/context.json
agent code context check --manifest /private/context/context.json
agent code context status --manifest /private/context/context.json --json
agent code context install --manifest /private/context/context.json --force
```

After choosing the canonical manifest, include drift in work readiness:

```bash
agent code preflight configure --context-manifest /private/context/context.json --require-context
```

Only the block between the generated-context markers is managed. Hand-written content outside the
block is preserved. The status includes a canonical corpus hash, explicit precedence, and overlap
findings for duplicate manual or foreign sources. Missing modules, partial markers, duplicate
markers, and stale generated content fail closed. A changed generated block is never overwritten
silently: inspect with `context check`, then use `context install --force`.

## Readiness states and failure classes

The stable readiness states are `ready`, `degraded`, `blocked`, and `unknown`. Typed failures use
`auth_missing`, `auth_expired`, `auth_wrong_identity`, `mcp_unauthed`, `network_unreachable`,
`dns_failure`, `permission_denied`, `config_missing`, `context_stale`, `source_unreachable`, or
`unknown`.
