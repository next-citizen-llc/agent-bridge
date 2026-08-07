# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Agent Bridge is **global local developer infrastructure** for bounded collaboration between
coding-agent CLIs (Claude Code, Codex, Grok, Anti-Gravity) from any project on the machine. It is
not a daemon and does not attach to running UI sessions — every operation is a fresh one-shot
headless turn or an append to shared on-disk state.

Four surfaces:
- **Bridge** (`agent code bridge`) — invoke one fresh headless turn of a target agent CLI for
  `review` or `code` work against a git worktree.
- **Mailbox MCP** (`agent_bridge/mailbox_mcp.py`) — a small shared mailbox + findings/verdicts store
  exposed as MCP tools for async handoff between agents.
- **Workflow engine** (`agent workflow`) — portable, structured multi-phase workflows run through a
  configured agent engine (e.g. `deep-research-lite`).
- **Session recovery** (`agent code sessions`) — inventory local native-session evidence and stage
  bounded, redacted continuation handoffs without importing one product's chat history into another.

## Commands

```bash
# Run the CLI from a checkout without installing (sets PYTHONPATH, execs the module)
bin/agent code bridge --list
bin/agent code sessions inventory --since-hours 168

# Dev checks (README-sanctioned; no third-party test runner)
python3 -m py_compile agent_bridge/*.py
python3 -m unittest discover -s tests

# A single test module / case / method
python3 -m unittest tests.test_mailbox
python3 -m unittest tests.test_coord.SomeTestCase
python3 -m unittest tests.test_workflow.SomeTestCase.test_something

# Install the global `agent` launcher symlink (~/.local/bin/agent -> bin/agent)
scripts/install.sh          # scripts/install.ps1 on Windows
```

There is no lint/format config and no Makefile. `pyproject.toml` exposes the console script
`agent = agent_bridge.cli:main_entry`.

## Hard constraints

- **Zero runtime dependencies.** `dependencies = []` in `pyproject.toml`, `requires-python >=3.11`,
  and the MCP server is deliberately "dependency-free stdio". Use only the Python standard library
  in `agent_bridge/`. Do not introduce third-party packages (no requests, no MCP SDK, etc.).
- **Runtime state lives outside the repo.** Bridge operational state writes under
  `~/.local/state/agent-bridge/` (override with `AGENT_BRIDGE_STATE_DIR`). The explicit exception is
  private cross-machine Codex state sync, which writes immutable chunks and per-machine metadata
  under the configured OneDrive `SharedAgentData/AgentBridgeStateSync/v1` root. `.gitignore` blocks
  `state/`, `transcripts/`, `mailbox/`, `bridge_agents.log`. Never commit runtime artifacts or add
  state paths inside the repo.
- **`review` mode is analysis-only; `code` mode may edit files.** Preserve this split when touching
  dispatch: review runs read-only / `--permission-mode auto` / `sandbox=read-only`; code runs
  `acceptEdits` / `workspace-write`. No live production actions, credentials, deploys, or direct
  GitHub pushes originate here.

## Architecture

### CLI dispatch is a hand-rolled prefix router — not top-level argparse subcommands
`agent_bridge/cli.py` `main()` (near the bottom, ~line 2572) matches `sys.argv` prefixes literally
(`argv[0] == "workflow"`, `argv[0] == "code" and argv[1] == "bridge"`, …) and delegates to a
per-verb function (`bridge`, `loop`, `repair_cmd`, `workflow_cmd`, `harness_cmd`, …). Each of those
functions builds *its own* `argparse` parser locally. **To add a command:** add a prefix branch in
`main()`, add a matching usage line in the `main()` help block, and write a `*_cmd`/verb function
that owns its argument parsing. `bin/agent` and `main_entry()` are the only entry points;
`main_entry` catches `BridgeError`/`SessionRecoveryError`/`WorkflowError`/`ValueError` and exits 2.

### Agents are declarative; adapters are the extension point
`agent_bridge/agents.json` defines each target agent with an `adapter` and a `command`
(overridable via env like `CLAUDE_BIN`, `CODEX_BIN`, `GROK_BIN`, `AGY_BIN`). Adapters:
- `claude_code`, `codex_exec` — bespoke command construction in `cli.py`.
- `argv` — generic; the JSON entry supplies `review_args` / `code_args` templates with
  placeholders `{prompt}`, `{scope}`, `{project_dir}`, `{source}` (see the `grok` and `agy` entries).

Per-adapter capabilities (modes, sandboxing) live in `ADAPTER_CAPABILITIES` in
`agent_bridge/coord.py`. **Adding a new agent is normally a JSON edit**, not code — reach for the
`argv` adapter first and only add a new adapter when a CLI needs custom argument shaping.

### Shared state modules (append-only JSONL)
`coord.py` is the backbone: `state_dir()`, JSONL read/append, task lifecycle, trace context
(`make_traceparent`/`current_trace_context`), policy evaluation + HMAC signing, and the transport
queue. `mailbox.py` (CLI mailbox) and `mailbox_mcp.py` (MCP server) read/write the same
`mailbox/messages.jsonl` so the shell tools and MCP interoperate. `findings.py` and `trace.py` back
`finding_*`/`verdict_*`/`trace_events`. Every module independently resolves the state dir with the
same `AGENT_BRIDGE_STATE_DIR` env pattern — keep that consistent if you add one.

`session_recovery.py` reads allowlisted metadata plus bounded user/assistant text from local Claude
indexes, stores private handoffs under `session-recovery/<run_id>/`, and optionally attaches them to
the task ledger. It must not copy raw transcripts, system prompts, account metadata, or tool-result
dumps into a recovery bundle.

`state_sync.py` is a separate, explicitly private Codex-history surface. It chunk-deduplicates
native sessions into `SharedAgentData`, publishes normalized project/thread indexes, and additively
merges them only while Codex Desktop is closed. It must not copy log databases, config, credentials,
or caches; it must not automatically delete or expire session objects.

### mailbox_mcp.py is a raw JSON-RPC stdio loop
No SDK. It implements `initialize`, `notifications/initialized`, `tools/list`, `tools/call` by hand
over stdin/stdout. Tools are declared in the `TOOLS` list with matching `_<tool>` handlers
(`mailbox_send`, `mailbox_inbox`, `mailbox_read`, `trace_events`, `finding_emit`, `findings_list`,
`finding_read`, `verdict_record`, `verdicts_list`). To add a tool: append its schema to `TOOLS` and
add a handler wired in the `tools/call` branch.

### Loop spawn-policy gate and budget auto-calibration
`agent code loop` defaults to `--spawn-policy auto`: it scores the prompt for implementation depth,
concrete scope, and risk before committing to the full builder/critic/verifier spend; a vague or
review-only prompt falls back to a single analysis-only adversarial agent
(`--spawn-policy full` / `adversarial-only` force the choice). Bridge and loop dispatches retry on
`Exceeded USD budget (...)`, calibrate the cap upward, and persist the working cap in
`~/.local/state/agent-bridge/connections.json` (`--no-budget-auto` / `--max-auto-budget-usd`).

### Cross-machine harness registry is a heartbeat store, not IPC
`agent code harness` reads/writes JSON rows in a shared OneDrive `SharedAgentSkills/Agent-Bridge`
folder. Root discovery cascades through `AGENT_BRIDGE_SHARED_SKILLS_ROOT`,
`SHARED_AGENT_SKILLS_ROOT`, `CAREER_SHARED_SKILLS_ROOT`, OneDrive env vars, then platform defaults.
A fresh row means a harness recently started and could see the folder — it does **not** prove a
session is idle or ready for work. Treat it as advisory.

## GitHub identity policy (enforced for this repo)

This repo pushes to `github.com/next-citizen-llc/agent-bridge`. Per the user's global policy: commits
are authored as Tristan Springmeyer, with **no** agent/assistant attribution — no `Co-Authored-By`
agent trailers, no "Generated with …" footers in commits/PRs/issues. Keep branch names, PR titles,
tags, and other repo-visible surfaces neutral and descriptive; never expose agent/tool identity in
them.
