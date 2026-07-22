# Active Session Recovery

Use this workflow when one harness reaches a usage, rate, or API limit while
several native sessions still contain unfinished work. The goal is to continue
the work without flattening projects, duplicating completed outputs, or
pretending that Claude and Codex native chat histories are interchangeable.

## Recovery Chain

1. Inventory the source application's visible sessions. The live sidebar is
   the authoritative answer to "which sessions are active"; local metadata
   alone cannot prove sidebar visibility.
2. Reconcile each visible session with its local metadata and transcript or
   audit-log pointer.
3. Classify each session as `continue` or operator-confirmed `complete`.
   Transcript order may identify a limit or unanswered turn, but it never
   proves completion by itself.
4. Verify the exact project or worktree, local Git state, and current GitHub PR
   state when a PR or repository artifact is part of the work.
5. Stage one bounded handoff and one durable task per unfinished session.
   Completed sessions are recorded but not re-dispatched.
6. In the target harness, create or claim one isolated native task per handoff.
   Re-verify time-sensitive state before changing files or external systems.

## 1. Inventory Claude Sessions

The inventory reads both Claude Code session metadata and Claude Desktop local
agent-mode metadata. It does not print prompt bodies, tool output, system
prompts, account details, or credentials.

```bash
agent code harness status --json
agent code sessions inventory --since-hours 168
```

Filter by a title or a source session id when the local store is large:

```bash
agent code sessions inventory --title "market data"
agent code sessions inventory --session-id local_01234567-89ab-cdef-0123-456789abcdef --json
```

Status signals are deliberately conservative:

- `blocked_usage_limit`: the newest unresolved turn contains a usage or rate
  limit signal.
- `blocked_api_error`: the newest unresolved turn contains another API error.
- `awaiting_assistant`: the latest user turn has no later successful assistant
  text.
- `review_required`: transcript order alone cannot establish whether the work
  is finished.
- `missing_transcript`: metadata was found but no matching transcript or audit
  log was found.

The live source UI still matters. A recent local record may be archived, hidden,
or unrelated to the visible set, while a visible session may need manual
inspection before its outcome is clear.

## 2. Review and Select

For a small recovery, pass reviewed decisions directly:

```bash
agent code sessions recover \
  --from claude \
  --to codex \
  --continue local_unfinished_a \
  --continue local_unfinished_b \
  --complete local_verified_complete \
  --project local_unfinished_a=/absolute/path/to/exact/worktree \
  --verify-github \
  --enqueue
```

For several sessions, use a private selection file outside the repository:

```json
{
  "sessions": [
    {
      "session_id": "local_unfinished_a",
      "disposition": "continue",
      "project_dir": "/absolute/path/to/exact/worktree"
    },
    {
      "session_id": "local_verified_complete",
      "disposition": "complete",
      "project_dir": "/absolute/path/to/repository"
    }
  ]
}
```

```bash
agent code sessions recover \
  --selection /private/path/recovery-selection.json \
  --to codex \
  --verify-github \
  --enqueue
```

`--complete` is an operator assertion. Use it only after checking the actual
artifact, target branch, submission receipt, or other source of record. The
workflow intentionally refuses to infer completion from phrases such as
"done" in a transcript.

Use `--project SESSION_ID=PATH` whenever metadata points to a parent directory,
an expired sandbox, or a different checkout. This override is how nested Git
repositories and exact existing worktrees remain isolated.

## 3. Artifacts and Tasks

Each run writes to:

```text
~/.local/state/agent-bridge/session-recovery/<run_id>/
  manifest.json
  summary.md
  sessions/
    <source-session-id>/
      evidence.json
      handoff.md          # continue decisions only
```

The bundle contains:

- native session identifiers and local evidence pointers;
- a bounded set of recent user/assistant text for continuations;
- common credential-shape redaction;
- exact project/worktree routing;
- local Git branch, HEAD, and dirty-path evidence;
- current PR metadata when `--verify-github` succeeds; and
- a target prompt that another harness can use to create an isolated native
  continuation.

It does not contain raw transcript copies, system prompts, tool-result dumps,
browser profiles, credentials, account metadata, or attachments.

`--enqueue` creates an idempotent Agent Bridge task keyed to the source session,
latest native activity cursor, and target harness, then attaches the handoff.
Running recovery again against the same source turn does not create a duplicate
task. A genuinely newer turn in the same native session receives a new task.
The workflow may attach fresher evidence to an existing open task.

An Agent Bridge task is not a native Codex or Claude chat. The target harness
must still create or claim one task per handoff in the correct project. This is
intentional: native UI task creation is harness-specific, while the recovery
bundle and task ledger remain portable.

## Safety Boundaries

- Keep recovery selections and generated bundles out of Git. They can contain
  private user context even after credential redaction. The recovery command
  rejects an output root located inside a Git worktree.
- Treat source paths as local evidence pointers, not portable paths across
  machines.
- Do not dispatch every recent metadata record automatically. Review the live
  source UI and classify first.
- Do not duplicate a completed session just because it appears beside an
  unfinished one.
- Do not overwrite, switch, clean, or reset a recovered worktree. Dirty paths
  are preservation evidence.
- GitHub verification is read-only. A missing `gh` login or unreachable remote
  is reported as unavailable and does not silently become proof of completion.
- Re-fetch market data, job status, CI, PR state, and other time-sensitive
  facts in the target task.

## Custom Roots and Tests

On non-default installations, set roots explicitly:

```bash
agent code sessions inventory \
  --claude-data-root /path/to/Claude/application-data \
  --claude-projects-root /path/to/.claude/projects
```

The equivalent environment variables are
`AGENT_BRIDGE_CLAUDE_DATA_ROOT` and
`AGENT_BRIDGE_CLAUDE_PROJECTS_ROOT`.
