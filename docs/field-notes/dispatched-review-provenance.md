# A dispatched review is not a completed review

When several reviewers are dispatched across harnesses in one round, the round's artifact tends to
record that reviews *happened*. The useful record is narrower: which dispatches produced usable
findings, which did not, and what the round therefore does not cover.

A four-dispatch round in July 2026 returned two usable reviews and two that could not be counted. The
artifact was only trustworthy because it said so explicitly.

## Outcome states for a dispatched review

| Outcome | Evidence | How it may be cited |
| --- | --- | --- |
| Completed, full scope | Reviewer ran and covered the requested scope | As coverage |
| Completed, bounded scope | Ran, but narrower than requested | Only for what it covered; the shortfall is recorded as a gap |
| Partial | Dispatched and attempted work, returned no completed findings report | Not as verification; any claim resting on it is downgraded |
| Blocked | Never ran — authentication, target resolution, or dispatch failure | Contributes nothing; recorded as blocked |

Collapsing these into "reviewed" or "not reviewed" is what lets an unearned claim of coverage survive
into a downstream artifact. A blocked dispatch is the dangerous case, because a review round that
names a reviewer reads as though that reviewer contributed.

## Authentication is per-surface, not per-client

In that round the same client was both blocked and productive: a CLI dispatch returned a logged-out
error and never ran, while the same client's authenticated interactive surface completed the work once
the task was relayed to it through the mailbox. Client identity says nothing about authentication
state; the surface does.

Two consequences. A bridge dispatch inherits the target's own session, so the repair is to restore
that session rather than to route around it. And when one surface is blocked, the mailbox is the
fallback relay to a surface that is already authenticated — the task moves, the authority does not.

This is why readiness reports per-surface authentication separately, and why an advisory unknown for a
surface the current process cannot inspect is not the same as a failure.

## Dispatched external retrieval is best effort; bounded local audits are not

The same reviewer was dispatched twice in that round. The live external-research pass attempted direct
source retrieval and returned no completed report. The bounded audit of named local files on disk
returned a severity-ranked result that was accepted in full.

Prefer bounded local-evidence tasks when dispatching a reviewer, and treat live external retrieval as
best effort that may return nothing. A round that depends on external retrieval for its central claim
should expect to record that claim as unverified.

## Record the gap, not just the result

Every accepted review should be paired with what it did not establish: scope the reviewer declined or
could not reach, checks not independently re-run, and negative results that are bounded rather than
conclusive. Absence of evidence found by one bounded pass is not evidence of absence, and the artifact
should say which one it is.

## Amend superseded records in place

Later evidence reversed two conclusions from that round. The record was amended with a supersession
notice at the top — do not act on the decisions below — and the body was left intact.

That is the right shape. Deleting or silently editing a superseded record destroys the ability to see
what was believed at the time and on what basis. A conclusion that was later reversed is still
accurate evidence about the review: it read the evidence available to it correctly, and the reversal
usually came from a signal no dispatch of that kind could have seen.

## Operator rule

Record a per-reviewer outcome state, not a round-level verdict. Never let a blocked or partial
dispatch contribute implied coverage, and keep superseded records with their supersession notice
rather than removing them.

Relevant implementation surfaces:

- [`agent_bridge/readiness.py`](../../agent_bridge/readiness.py)
- [`agent_bridge/mailbox.py`](../../agent_bridge/mailbox.py)
- [`agent_bridge/findings.py`](../../agent_bridge/findings.py)
- [`agent_bridge/trace.py`](../../agent_bridge/trace.py)
- [A failure taxonomy for cross-harness workflows](failure-taxonomy.md)
