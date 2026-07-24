# Verifying an agent handoff without sharing private transcripts

A useful handoff should let the receiver verify current state without receiving the source product's
entire conversation history.

Raw transcripts are tempting because they contain context, but they also contain irrelevant private
material, stale reasoning, account details, tool output, and instructions that should not become
authority in the next system. A bounded handoff should transfer the minimum state needed to continue
and provide pointers for independent verification.

## What to include

- source harness and local session identifier;
- exact project path, repository remote, branch, and head revision;
- concise user objective and acceptance criteria;
- authority boundaries and prohibited actions;
- files intentionally changed;
- commands run and their bounded results;
- unresolved findings and next safe action;
- hashes or source pointers for artifacts that matter;
- timestamp and freshness limit.

## What to exclude

- raw system prompts or full chat transcripts;
- credentials, tokens, cookies, signed URLs, and account metadata;
- unrelated personal or repository content;
- tool-result dumps that the receiver can regenerate;
- conclusions presented as facts without source pointers;
- implied permission inherited from conversational tone.

## Receiver verification

Before continuing, the receiver should:

1. confirm the project path and Git remote;
2. inspect the current branch, head, status, and diff;
3. re-open every acceptance criterion and authoritative source;
4. check whether any cited live state has drifted;
5. restate the authority boundary;
6. run the narrowest check that proves the next action is still appropriate.

If the source and current states disagree, current authoritative state wins and the mismatch becomes
a finding.

## How Agent Bridge applies the pattern

`agent code sessions inventory` reads allowlisted local session evidence. `agent code sessions
recover` writes bounded, credential-redacted continuation bundles under the local Agent Bridge state
directory and can attach them to a durable task. It does not import one product's native chat history
or claim that a destination UI task exists.

Relevant implementation and documentation:

- [`agent_bridge/session_recovery.py`](../../agent_bridge/session_recovery.py)
- [Active session recovery](../active-session-recovery.md)

The broader principle is simple: transfer verifiable state and explicit authority, not conversational
exhaust.
