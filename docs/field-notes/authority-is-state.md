# Authority should be explicit state, not prompt prose

Prompts are useful for intent. They are a poor place to store authority.

A sentence such as "please review this safely" is ambiguous about files, tools, credentials,
external systems, and what happens after the review. It can also be summarized, reordered, or
competed with by instructions found in a repository. A durable workflow needs an authority decision
that can be inspected without interpreting the model's prose.

Agent Bridge makes this distinction visible in three places:

- the operator selects `review` or `code`;
- the adapter turns that mode into target-specific sandbox and permission flags;
- the dispatched contract and trace record what the turn was allowed to do.

The prompt still matters, but it describes the task inside the boundary. It does not create the
boundary.

## A practical authority record

The exact schema will vary, but a useful record answers these questions before execution:

```json
{
  "mode": "review",
  "project_root": "/absolute/path/to/worktree",
  "writes_allowed": false,
  "external_actions_allowed": false,
  "credential_use_allowed": false,
  "requested_by": "human",
  "run_id": "run_...",
  "expires_after_turn": true
}
```

This example is illustrative, not an Agent Bridge wire format. Its value is that a launcher,
reviewer, and later auditor can reach the same answer without guessing what "safely" meant.

## Why prompt-only authority fails

1. **It is not machine-checkable.** A launcher cannot reliably distinguish a preference from a hard
   prohibition buried in prose.
2. **It drifts across handoffs.** Summaries preserve the topic more reliably than the exact authority
   boundary.
3. **It conflates capability and permission.** A tool being able to push, deploy, or send does not
   mean the current task permits it.
4. **It produces weak receipts.** A transcript can show what the model said; it may not show which
   sandbox, worktree, or external-action policy actually applied.

## Design rule

Put authority into typed state and enforcement mechanisms. Echo it in the prompt for model
comprehension. Record it in traces for audit. Require a separate, explicit transition when the
authority changes.

Relevant implementation surfaces:

- [`agent_bridge/cli.py`](../../agent_bridge/cli.py) builds mode-specific commands and contracts.
- [`agent_bridge/coord.py`](../../agent_bridge/coord.py) records task, trace, and policy state.
- [`agent_bridge/agents.json`](../../agent_bridge/agents.json) declares target adapters and flags.

The general pattern applies beyond coding agents: approvals, outbound messages, production changes,
payments, and destructive operations should all be explicit state transitions rather than
implications inferred from conversational momentum.
