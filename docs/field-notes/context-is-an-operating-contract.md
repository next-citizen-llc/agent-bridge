# Context is an operating contract, not a prompt payload

More context does not automatically produce better work. A larger prompt can mix current facts with
stale assumptions, bury authority boundaries, and make it difficult to tell which evidence actually
shaped a decision.

A governed workflow needs an operating-context contract: a bounded statement of what the turn is
trying to accomplish, what state and sources it may rely on, what it may do, and what evidence must
exist when it stops.

The contract should survive summarization and handoff. It should also be inspectable without asking
a model to explain which parts of a long conversation it treated as important.

## Five parts of useful operating context

1. **Objective and decision.** State the concrete outcome and the decision the work should enable.
   A topic such as "review authentication" is not yet an objective.
2. **Source authority.** Name the files, systems, live checks, or people that can establish current
   truth. Separate authoritative state from background material and inference.
3. **Working state.** Record the project root, branch, revision, relevant artifact versions, and
   freshness limits. Context without time and version boundaries decays silently.
4. **Action boundary.** Distinguish available capabilities from current permission. Specify write,
   credential, external-action, and destructive-operation authority explicitly.
5. **Exit evidence.** Define the tests, receipts, artifacts, or unresolved findings required at the
   end of the turn. A plausible answer is not the same as a verified result.

An illustrative contract might look like this:

```json
{
  "objective": "Determine whether the proposed change is ready to merge",
  "decision": "merge, revise, or block",
  "authoritative_sources": [
    "current worktree",
    "target-branch checks",
    "acceptance criteria"
  ],
  "working_state": {
    "project_root": "/absolute/path/to/worktree",
    "branch": "feature-branch",
    "head": "0123456789abcdef",
    "observed_at": "2026-07-27T00:00:00Z"
  },
  "authority": {
    "mode": "review",
    "writes_allowed": false,
    "external_actions_allowed": false
  },
  "exit_evidence": [
    "findings with file and line references",
    "check results",
    "explicit verdict"
  ]
}
```

This is a design example, not an Agent Bridge wire format.

## Where process intelligence enters

The contract defines the state needed to begin. A trace shows how work moved through that state.
Together they create a small process-intelligence loop:

```text
objective -> evidence consulted -> decision -> action -> result -> exception or next state
```

That sequence is more useful than a transcript when a team needs to understand why work stalled,
which authority boundary prevented an action, where evidence went stale, or whether a verifier
actually checked the result.

The goal is not to record every token. It is to preserve consequential transitions and enough
evidence to reproduce the decision.

## How Agent Bridge maps to the contract

Agent Bridge exposes parts of this model across existing surfaces:

- `agent code bridge` records the selected project, branch, mode, and dispatch contract;
- readiness checks describe whether required binaries, authentication, repository identity, and
  context are currently available;
- task and trace state provide correlation and lifecycle evidence;
- mailbox findings and verdicts carry structured review outcomes;
- session recovery stages bounded continuation state without importing a private transcript.

These surfaces do not make every workflow governed automatically. The operator remains responsible
for choosing authoritative sources, setting the correct action boundary, and deciding what evidence
is sufficient. Agent Bridge makes those decisions easier to state and inspect.

## Common failure modes

- **Transcript as source of truth.** Conversation history contains useful clues, but current files
  and live systems may have changed since the discussion.
- **Context without expiry.** A correct readiness result or repository revision becomes misleading
  after the environment moves.
- **Capability treated as permission.** A configured tool can be available while its use remains
  outside the authority of the current turn.
- **Output without a receipt.** A summary can sound complete even when no test, live check, or
  artifact establishes the result.
- **Handoff without re-verification.** The receiver inherits a conclusion instead of checking the
  current authoritative state.

## Review checklist

Before dispatch:

- Is the objective concrete enough to decide when to stop?
- Are authoritative sources named separately from background context?
- Are project, branch, revision, and observation time recorded?
- Are write and external-action permissions explicit?
- Is the required exit evidence defined?

Before accepting the result:

- Can another reviewer reproduce the decisive checks?
- Does the trace show meaningful state transitions rather than raw conversational volume?
- Are exceptions and unresolved findings visible?
- Has drift-prone state been refreshed?
- Does the handoff transfer evidence and authority without transferring private transcript exhaust?

Related notes:

- [Authority should be explicit state, not prompt prose](authority-is-state.md)
- [Verifying an agent handoff without sharing private transcripts](verifiable-handoffs.md)
- [A failure taxonomy for cross-harness workflows](failure-taxonomy.md)
