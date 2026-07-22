# Agent Bridge

Global local bridge for bounded collaboration between coding agents from any project.

The bridge has four surfaces:

- `agent code bridge` invokes a fresh headless turn of a configured agent CLI for review or
  local coding work.
- `mailbox_mcp.py` exposes a small shared mailbox as MCP tools for async handoff.
- `agent workflow` runs portable, structured workflows through configured agent engines.
- `agent code sessions` inventories native Claude session evidence and stages bounded continuation
  handoffs without importing chat history.

The bridge is local developer infrastructure. It is not a daemon and it does not attach to
existing UI sessions.

## Install

```bash
cd ~/Code/agent-bridge
scripts/install.sh
```

That creates:

```text
~/.local/bin/agent -> ~/Code/agent-bridge/bin/agent
```

`~/.local/bin` must be on `PATH`.

On Windows PowerShell:

```powershell
git clone https://github.com/tristan-nextcz/agent-bridge.git $HOME\Code\agent-bridge
cd $HOME\Code\agent-bridge
.\scripts\install.ps1
```

That creates:

```text
%USERPROFILE%\.local\bin\agent.cmd
%USERPROFILE%\.local\state\agent-bridge\
```

The installer adds `%USERPROFILE%\.local\bin` to the user `PATH` unless you pass
`-SkipPathUpdate`. Open a new terminal after install so `agent` is available everywhere.

## Configure MCP

Register the mailbox globally in Claude Code:

```bash
claude mcp add --scope user mailbox -- python3 ~/Code/agent-bridge/agent_bridge/mailbox_mcp.py
```

Register the mailbox globally in Codex:

```bash
codex mcp add mailbox -- python3 ~/Code/agent-bridge/agent_bridge/mailbox_mcp.py
```

The mailbox tools are:

- `mailbox_send`
- `mailbox_inbox`
- `mailbox_read`
- `trace_events`
- `finding_emit`
- `findings_list`
- `finding_read`
- `verdict_record`
- `verdicts_list`

## Configure Session Hooks

Install lightweight SessionStart hooks and startup wrappers for active harness surfaces:

```bash
agent code hooks install --client all
```

The hook first runs a bounded automatic refresh of the canonical Agent Bridge checkout, then
injects a short reminder that Agent Bridge is available and writes a shared registry heartbeat.
It does not prove authentication, spawn agents, or mutate the active project. Claude remains
declared but is inactive by default; install it explicitly with `--client claude` when needed.

## Automatic Updates

Every installed SessionStart hook and GUI wrapper checks the canonical checkout before the
harness registers itself. Checks are cached for five minutes by default. A refresh proceeds only
when all of these conditions hold:

- the checkout is on `main` and has no local changes;
- `origin` is the configured canonical GitHub repository;
- `origin/main` is a strict fast-forward of the local revision; and
- Git can fetch without an interactive credential prompt.

When the revision changes, the updater compiles the Python package, refreshes the `agent`
launcher, reinstalls hooks and wrappers, refreshes the shared skill and its Codex, Claude, Grok,
and `.agents` links, and re-executes the startup hook once so the new code supplies session
context. Dirty, non-main, ahead, or diverged checkouts are left untouched. Offline startup uses
the last installed revision and briefly caches the failed fetch.

Inspect or run it directly:

```bash
agent code update status
agent code update check
agent code update apply --force
```

Set `AGENT_BRIDGE_DISABLE_AUTO_UPDATE=1` for an emergency startup bypass. Override the normal
cache and fetch bounds with `AGENT_BRIDGE_UPDATE_INTERVAL_SECONDS` and
`AGENT_BRIDGE_UPDATE_TIMEOUT`.

Check hook status:

```bash
agent code hooks status --client all --json
```

The surface manifest also defines wrappers for harnesses without native lifecycle hooks. See
[`docs/readiness-and-context.md`](docs/readiness-and-context.md) for the exact native, wrapper,
service-probe, and unsupported states.

Install or refresh the shared Agent Bridge skill package and link it into local harness skill
roots:

```bash
agent code harness install-skill
```

On Windows, `.\scripts\install.ps1` runs the same hook installer automatically. To also attempt
MCP registration for both local CLIs, run:

```powershell
.\scripts\install.ps1 -RegisterMcp
```

## Use

From any git worktree:

```bash
agent code bridge --from human --to claude --mode review \
  --prompt "Review the current diff for concrete defects."

agent code bridge --from human --to codex --mode code \
  --prompt "Implement the scoped change and run focused tests."

agent code bridge --from human --to grok --mode review \
  --prompt "Review the current diff for concrete defects."

agent code bridge --from human --to agy --mode code \
  --prompt "Implement the scoped change and run focused tests."
```

The bridge targets the current git root by default. Use `--project-dir` to target a different
worktree:

```bash
agent code bridge --project-dir /path/to/repo --from human --to claude --mode review \
  --prompt "Review this release checklist."
```

List configured agents:

```bash
agent code bridge --list
```

Agent commands are resolved from `PATH` by default. Override local CLI paths with
`CLAUDE_BIN`, `CODEX_BIN`, `GROK_BIN`, or `AGY_BIN` when a harness is installed outside `PATH`.

Dry-run without invoking model CLIs:

```bash
agent code bridge --from human --to claude --mode review --dry-run \
  --prompt "Show the command you would run."
```

HEIC/HEIF image paths in bridge or loop prompts are converted to PNG automatically when the
source file exists. The converted copies are stored under:

```text
~/.local/state/agent-bridge/media/
```

The bridge appends an `[AGENT BRIDGE MEDIA]` note with the PNG path to the dispatched prompt.
Claude Code targets also receive the media cache through `--add-dir` so the converted image is
readable. On macOS the default converter is `sips`; otherwise the bridge looks for ImageMagick
`magick` or `convert`. To supply a custom converter, set `AGENT_BRIDGE_HEIC_CONVERTER` to a
command prefix; the source path and output path are appended as the final two arguments.

Repair and calibrate target connectivity:

```bash
AGENT_BRIDGE_CLAUDE_EMAIL=you@example.com agent code repair --to claude
```

The repair command checks Claude auth status, runs a direct non-interactive Claude probe, refreshes
Claude login if the probe returns a 401, and then runs a real bridge handshake. It starts with a
small budget and calibrates upward when the CLI returns `Exceeded USD budget (...)`. Normal
`agent code bridge` and `agent code loop` dispatches also retry budget failures automatically and
store the successful cap under:

```text
~/.local/state/agent-bridge/connections.json
```

Use `--no-budget-auto` to disable retry/calibration for a specific bridge or loop call, or
`--max-auto-budget-usd` to cap automatic retries.

Run a bounded adversarial loop:

```bash
agent code loop --builder codex --critic claude --verifier claude --max-turns 1 \
  --prompt "Implement the scoped change and look for blocking defects."
```

By default, `agent code loop` uses `--spawn-policy auto`. The bridge scores the prompt for
implementation depth, concrete scope, and risk signals before spending on the full
builder/critic/verifier loop. If the request is vague, review-only, or too shallow to justify a
full spawn, it dispatches one analysis-only adversarial agent instead. Use
`--spawn-policy full` to force the full loop, or `--spawn-policy adversarial-only` to always run
the single-review fallback.

Inspect cross-machine harness registrations from the shared OneDrive folder:

```bash
agent code harness status
agent code harness status --json
agent code harness register --client codex
```

The shared registry is a OneDrive-friendly heartbeat store, not a daemon and not direct IPC. A
fresh row means that a harness on that machine recently started or resumed and could see the
shared folder. It does not prove that an existing UI session is idle, authenticated, or ready to
accept work.

Run a cache-safe session check or a deeper authenticated work check:

```bash
agent code preflight session --client codex --surface gui
agent code preflight work --client grok --surface bridge --expected-github-login YOUR_LOGIN
agent code preflight work --client ollama --surface local-api --expected-github-login YOUR_LOGIN
```

Bridge and loop dispatches use cache-first bounded work preflight. Blocked required checks refuse
code dispatch; degraded reviews continue with a traced warning. Use `--require-ready` for a strict
all-green gate, `--refresh-readiness` for a bounded live refresh, or `--no-preflight` for an
explicit traced operator override. Readiness reports distinguish client/surface/machine/project
and use typed failures for auth, MCP, network, configuration, context, and source problems.
Generate harness context files from a private canonical manifest with `agent code context install`,
detect drift and overlaps with `agent code context check`, and use `--force` only after reviewing a
stale generated block.

Run portable deep research with a consistent command and output shape:

```bash
agent workflow list
agent workflow show deep-research-lite
agent workflow run deep-research-lite --engine codex --tier shallow \
  --question "What changed in Python 3.13?"
agent workflow inspect --run-id run_...
```

`agent workflow run` defaults the engine from `--engine`, then `--from` or
`AGENT_BRIDGE_CALLER`, and falls back to `codex`. It prints a Markdown report by default and
stores `manifest.json`, `report.md`, `result.json`, per-call prompts/responses, and fetched
source excerpts under:

```text
~/.local/state/agent-bridge/workflows/<run_id>/
```

Inspect trace events and structured findings:

```bash
agent code trace --run-id run_...
agent code findings create --run-id run_... --severity high --claim "..."
agent code verdicts record --run-id run_... --status fail --summary "..."
```

Recover unfinished work when a native harness reaches a usage or API limit:

```bash
agent code sessions inventory --since-hours 168
agent code sessions recover \
  --from claude \
  --to codex \
  --continue local_source_session_id \
  --project local_source_session_id=/absolute/path/to/exact/worktree \
  --verify-github \
  --enqueue
```

Inventory is metadata-only. Recovery writes bounded, credential-redacted handoffs under private
Agent Bridge state and can create an idempotent durable task for each unfinished source session.
It does not import native chat history or claim that a Codex/Claude UI task was created. Review the
live source sidebar first, mark proven completed sessions with `--complete`, then create or claim one
isolated target task per continuation. See
[`docs/active-session-recovery.md`](docs/active-session-recovery.md).

## State

Runtime state is outside repositories:

```text
~/.local/state/agent-bridge/
  bridge_agents.log
  events.jsonl
  findings.jsonl
  verdicts.jsonl
  transcripts/
  session-recovery/<run_id>/
  mailbox/messages.jsonl
```

Cross-machine status lives in the shared skills folder when configured:

```text
SharedAgentSkills/
  Agent-Bridge/
    SKILL.md
    registry/
      <machine>.<client>.json
```

Root discovery checks `AGENT_BRIDGE_SHARED_SKILLS_ROOT`, `SHARED_AGENT_SKILLS_ROOT`,
`CAREER_SHARED_SKILLS_ROOT`, OneDrive environment variables, then the platform defaults.
`SharedAgentData` and `SharedAgentConversations` are resolved independently. Configure a durable,
explicit split mapping with `agent code preflight roots` and its `--set-skills`, `--set-data`, and
`--set-conversations` options.

To copy Codex Desktop sessions and sidebar workspaces between machines, use
[`docs/codex-sidebar-state-sync.md`](docs/codex-sidebar-state-sync.md).

For the proposed always-on light sync layer across harness start/exit, active work,
and idle OS scheduler runs, see
[`docs/design/always-on-state-sync.md`](docs/design/always-on-state-sync.md).

Override with:

```bash
export AGENT_BRIDGE_STATE_DIR=/path/to/state
```

## Safety

- No live production actions, credential use, deploys, teardowns, or direct GitHub pushes.
- `review` mode is analysis-only.
- `code` mode may edit local files in the target worktree; review diffs before committing.
- Keep public branch names, PR titles, and repo-visible artifacts neutral and logical. Do not
  expose agent/tool identity in branch names.

## Development

```bash
python3 -m py_compile agent_bridge/*.py
python3 -m unittest discover -s tests
```
