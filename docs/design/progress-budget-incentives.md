# Progress Budget Incentives

Agent Bridge now treats target-agent budget as a bounded control signal rather than a blank check. A target can earn a larger retry budget only after observable progress appears in stdout status markers or local worktree polling.

## Research Cues

- AutoGen frames multi-agent systems as customizable, conversable agents with flexible behavior patterns across natural language and code. That supports keeping the bridge generic and policy-driven rather than baking in one target agent or one workflow shape: https://www.microsoft.com/en-us/research/publication/autogen-enabling-next-gen-llm-applications-via-multi-agent-conversation-framework/
- ChatDev uses staged communication and role-specific dialogue for software development. That supports splitting broad code-mode handoffs into smaller issue-scoped slices instead of one giant synchronous run: https://arxiv.org/abs/2307.07924
- AgentCoder separates programmer, test designer, and executor roles, and reports lower token overhead than heavier multi-agent designs. That supports rewarding externally visible evidence, especially tests and changed files, rather than self-reported effort alone: https://arxiv.org/html/2312.13010v3
- Reflexion treats scalar or free-form feedback as verbal reinforcement without model fine-tuning. That maps to bridge progress signals: status markers, percent complete, samples, and test output can guide the next retry budget: https://arxiv.org/abs/2303.11366
- SWE-agent emphasizes agent-computer interface design and structured feedback for repository-level coding. That supports first-class transcripts, timeouts, worktree polling, and clear termination records: https://arxiv.org/abs/2405.15793

## Incentive Contract

Progress is promising only when it is inspectable:

- A target emits a line such as `[agent-bridge-status] {"percent_complete": 60, "sample": "changed files: agent_bridge/cli.py"}`.
- Stdout contains clear implementation or verification signals, such as files changed, tests passing, or a code/diff sample.
- The worktree status or diff changes after dispatch.

When a budget cap is hit after promising progress, the bridge may jump by `--progress-bonus-usd` on retry, still capped by `--max-auto-budget-usd`. If no progress is seen, the ordinary budget ladder applies.

## Guardrails

- Wall-clock and idle-output limits remain hard safety rails.
- Worktree progress resets idle accounting but never extends the wall-clock cap.
- A progress reward never exceeds `--max-auto-budget-usd`.
- Broad code-mode prompts with several issue refs are split into issue slices by default, with task-ledger and trace events for each slice.
- Progress samples are capped and stored in trace metadata, not treated as proof of correctness.
