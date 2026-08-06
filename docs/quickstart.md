# External quickstart

This path starts from a new machine or directory and reaches one bounded review turn.

## Prerequisites

- Git
- Python 3.11 or newer
- One supported target CLI installed and authenticated for the real invocation

Agent Bridge currently declares adapters for Codex, Claude Code, Grok, and Anti-Gravity. Listing and
dry-run inspection work even when no target CLI is authenticated.

## 1. Clone and install

macOS or Linux:

```bash
git clone https://github.com/next-citizen-llc/agent-bridge.git
cd agent-bridge
scripts/install.sh
export PATH="$HOME/.local/bin:$PATH"
```

Windows PowerShell:

```powershell
git clone https://github.com/next-citizen-llc/agent-bridge.git $HOME\Code\agent-bridge
cd $HOME\Code\agent-bridge
.\scripts\install.ps1
$env:Path = "$HOME\.local\bin;$env:Path"
```

The installer adds only a collision-safe launcher. It does not edit a shell profile, persist a PATH
change, install startup hooks, or enable automatic updates. On Windows, use
`.\scripts\install.ps1 -UpdatePath` only when a persistent user PATH change is wanted.

## 2. Inspect the configured adapters

```bash
agent code bridge --list
```

This lists adapter definitions. It does not prove that each target is installed, authenticated, or
ready.

## 3. Inspect a turn before running it

From any Git worktree:

```bash
agent code bridge \
  --from human \
  --to codex \
  --mode review \
  --dry-run \
  --prompt "Identify one concrete documentation ambiguity."
```

Confirm the printed project, branch, target executable, review contract, and read-only sandbox.

## 4. Run a readiness check

```bash
agent code preflight work --client codex --surface bridge
```

Readiness is evidence about a specific client, surface, machine, and moment. A degraded review may
still be allowed; use `--require-ready` on a bridge or loop command when every required check must
be green.

## 5. Run the review

```bash
agent code bridge \
  --from human \
  --to codex \
  --mode review \
  --prompt "Identify one concrete documentation ambiguity."
```

Review mode is analysis-only. To authorize local worktree edits, choose `--mode code` and state the
exact implementation scope:

```bash
agent code bridge \
  --from human \
  --to codex \
  --mode code \
  --prompt "Clarify the installation prerequisite in README.md and run the documentation checks."
```

## Verify the installation

From the Agent Bridge checkout:

```bash
python3 -m py_compile agent_bridge/*.py
python3 -m unittest discover -s tests
```

The runtime package has no third-party dependencies. Build tooling may resolve `setuptools` when
installing from `pyproject.toml`.

## Uninstall

Unix:

```bash
scripts/uninstall.sh
scripts/uninstall.sh --remove-hooks
```

Windows PowerShell:

```powershell
.\scripts\uninstall.ps1
.\scripts\uninstall.ps1 -RemoveHooks
```

The first form removes only the launcher when it still matches this checkout. The second also
removes exact Agent Bridge hook and wrapper entries. Modified or unrelated launchers and wrappers
are preserved. Runtime records remain under `~/.local/state/agent-bridge/` until the operator
deliberately removes them.

## Optional mailbox MCP

From the cloned repository:

```bash
MCP_PYTHON="${AGENT_BRIDGE_PYTHON:-python3}"
"$MCP_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
codex mcp add mailbox -- "$MCP_PYTHON" "$PWD/agent_bridge/mailbox_mcp.py"
claude mcp add --scope user mailbox -- "$MCP_PYTHON" "$PWD/agent_bridge/mailbox_mcp.py"
```

Registration changes the selected CLI's user configuration and is therefore separate from the
launcher install.
