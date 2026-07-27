# Agent Bridge

Local-first CLI and MCP infrastructure for bounded collaboration across coding-agent tools.

Created by [Tristan Springmeyer (`@tristan-nextcz`)](https://github.com/tristan-nextcz) and
organization-owned by [Next Citizen LLC](https://github.com/next-citizen-llc) for durable governance.

Agent Bridge launches fresh headless turns, records local coordination state, and makes the
authority of each turn explicit. It does not attach to an existing chat, run as a daemon, or turn
one product's private conversation history into another product's history.

## What it provides

- `agent code bridge` — one analysis-only or workspace-writing turn through a configured CLI.
- `agent code loop` — a bounded builder, critic, and verifier sequence with a spend-aware spawn
  gate.
- `agent workflow` — portable multi-phase workflows with stored manifests and results.
- `mailbox_mcp.py` — a dependency-free local JSON-RPC mailbox, findings, verdict, and trace store.
- `agent code sessions` — bounded, redacted continuation handoffs from local session evidence.

The Python runtime uses the standard library only and requires Python 3.11 or newer.

## 90-second demo

This dry run invokes no model and makes no repository changes:

```bash
git clone https://github.com/next-citizen-llc/agent-bridge.git
cd agent-bridge

bin/agent code bridge --list
bin/agent code bridge \
  --from human \
  --to codex \
  --mode review \
  --dry-run \
  --prompt "Review README.md for one unclear setup step."
```

The output shows the resolved executable, project, branch, correlation IDs, analysis-only contract,
and sandbox mode before any target CLI runs.

With a supported CLI installed and authenticated, remove `--dry-run` to execute the review:

```bash
bin/agent code bridge \
  --from human \
  --to codex \
  --mode review \
  --prompt "Review README.md for one unclear setup step."
```

See the [external quickstart](docs/quickstart.md) for installation and the first real run.

## Install

macOS or Linux:

```bash
git clone https://github.com/next-citizen-llc/agent-bridge.git
cd agent-bridge
scripts/install.sh
export PATH="$HOME/.local/bin:$PATH"
agent code bridge --list
```

Windows PowerShell:

```powershell
git clone https://github.com/next-citizen-llc/agent-bridge.git $HOME\Code\agent-bridge
cd $HOME\Code\agent-bridge
.\scripts\install.ps1
$env:Path = "$HOME\.local\bin;$env:Path"
agent code bridge --list
```

The installer creates a collision-safe launcher under the current user's `.local/bin`. It does not
edit shell startup files, persist a PATH change, install hooks, or enable automatic updates by
default. Use `--force` on Unix or `-Force` on Windows only after inspecting an existing launcher.

A target CLI such as Codex, Claude Code, Grok, or Anti-Gravity must be installed separately for a
real bridge call.

## Optional integrations

Startup hooks and wrappers are explicit opt-ins. They register harness context and run the bounded
Agent Bridge updater when those surfaces start:

```bash
agent code hooks install --client all
agent code hooks status --client all
```

Remove only exact Agent Bridge hook and wrapper entries with:

```bash
agent code hooks uninstall --client all
```

Register the local mailbox MCP with either supported CLI:

```bash
codex mcp add mailbox -- python3 "$PWD/agent_bridge/mailbox_mcp.py"
claude mcp add --scope user mailbox -- python3 "$PWD/agent_bridge/mailbox_mcp.py"
```

Run those commands from the cloned Agent Bridge repository so the registered path is absolute and
stable.

## Common operations

Run one bounded code turn against the current Git worktree:

```bash
agent code bridge --from human --to codex --mode code \
  --prompt "Implement the scoped change and run focused tests."
```

Run a bounded adversarial review:

```bash
agent code loop --builder codex --critic claude --verifier claude --max-turns 1 \
  --spawn-policy adversarial-only \
  --prompt "Challenge the current artifact against its stated acceptance criteria."
```

Run a portable workflow:

```bash
agent workflow list
agent workflow run deep-research-lite --engine codex --tier shallow \
  --question "What changed in Python 3.13?"
```

Inspect recent native-session evidence without importing raw chat history:

```bash
agent code sessions inventory --since-hours 168
```

Agent Bridge targets the current Git root by default. Use `--project-dir /absolute/path` to target a
different checkout.

## Architecture and safety

- [Architecture](docs/architecture.md) explains the CLI router, adapters, local state, mailbox, and
  workflow boundaries.
- [Safety model](docs/security-model.md) states what the bridge enforces, what it merely records,
  and what remains the operator's responsibility.
- [Readiness and context](docs/readiness-and-context.md) documents live preflight states and
  generated harness context.
- [Active session recovery](docs/active-session-recovery.md) documents bounded continuation
  handoffs.

Agent Bridge is a local-first control plane, not a local-inference guarantee. Prompts sent to a
vendor CLI may still be transmitted under that tool's own account, privacy, and retention terms.

## Field notes

- [Context is an operating contract, not a prompt payload](docs/field-notes/context-is-an-operating-contract.md)
- [Authority should be explicit state, not prompt prose](docs/field-notes/authority-is-state.md)
- [A failure taxonomy for cross-harness workflows](docs/field-notes/failure-taxonomy.md)
- [Verifying an agent handoff without sharing private transcripts](docs/field-notes/verifiable-handoffs.md)

## Development

```bash
python3 -m py_compile agent_bridge/*.py
python3 -m unittest discover -s tests
```

Runtime state is stored outside the repository under `~/.local/state/agent-bridge/` by default. Set
`AGENT_BRIDGE_STATE_DIR` to use another local location.

## Uninstall

```bash
scripts/uninstall.sh
scripts/uninstall.sh --remove-hooks
```

Windows equivalents are `.\scripts\uninstall.ps1` and
`.\scripts\uninstall.ps1 -RemoveHooks`. Uninstallers remove only launchers and generated hook
entries that exactly match this checkout. Local runtime evidence is retained unless the operator
removes it separately.

## Release

The first public package is [v0.1.0](https://github.com/next-citizen-llc/agent-bridge/releases/tag/v0.1.0).

## License

[MIT](LICENSE)
