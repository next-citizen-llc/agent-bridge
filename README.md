# Agent Bridge

Global local bridge for bounded collaboration between coding agents from any project.

The bridge has two surfaces:

- `agent code bridge` invokes a fresh headless turn of a configured agent CLI for review or
  local coding work.
- `mailbox_mcp.py` exposes a small shared mailbox as MCP tools for async handoff.
- `agent workflow` runs portable, structured workflows through configured agent engines.

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

Install lightweight SessionStart hooks for both Codex and Claude:

```bash
agent code hooks install --client both
```

The hook injects a short reminder that Agent Bridge is available, points agents at the global
mailbox MCP path, and notes that `agent code loop` uses the auto dispatch gate. If a shared
OneDrive `SharedAgentSkills/Agent-Bridge` folder is available, it also writes a small JSON
heartbeat for the current harness. It does not spawn agents, run network calls, or mutate project
files during session startup.

Check hook status:

```bash
agent code hooks status --client both
```

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

## State

Runtime state is outside repositories:

```text
~/.local/state/agent-bridge/
  bridge_agents.log
  events.jsonl
  findings.jsonl
  verdicts.jsonl
  transcripts/
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
