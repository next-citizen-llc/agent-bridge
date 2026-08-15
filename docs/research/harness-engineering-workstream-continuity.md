# Harness engineering for cross-machine work continuity

**Reviewed:** 2026-08-07
**Decision:** Treat a workstream report as a view over durable, verifiable
project state. Do not treat it as transcript synchronization, readiness proof,
or implicit remote execution.

## Scope and evidence standard

This note asks what should make work resumable when it moves among models,
harnesses, repositories, and computers. The research inputs included AI Dev
Daily, a private AI-research curation, and current public repositories selected
from that curation and the operator's GitHub stars. Only public-safe,
independently accessible sources are cited here. Private captures and notes
were discovery inputs, not published evidence.

Source quality is intentionally uneven:

- **Primary implementation or documentation:** strongest support for what a
  system implements or its authors observed.
- **Research preprint:** useful formalization, not peer-reviewed proof.
- **Project self-description:** evidence of a design pattern, not of efficacy.
- **Practitioner article:** discovery signal only unless corroborated.

AI Dev Daily is in the last category. Its Antigravity article usefully points
to task lists, implementation plans, walkthroughs, browser validation, and
multi-agent workspace management, but it supplies no evaluation method. Its
Codex review likewise recommends tests and human oversight but repeats vendor
claims without a reproducible evidence package. Those articles informed the
questions; they do not carry load-bearing conclusions.

## Findings

### 1. Give work a stable identity outside any native chat

A resumable unit needs an objective-level identifier, project/worktree,
repository revision, current state, acceptance evidence, authority boundary,
and next safe action. Native session IDs remain useful evidence pointers, but
they are vendor-specific and cannot be the cross-harness identity.

This follows the broader harness framing that task specification, project
memory, task state, observability, verification, permissions, and intervention
recording are separate runtime responsibilities. The proposed trace-based
evaluation unit is an auditable episode package, not merely a final patch.

**Agent Bridge implication:** the catalog owns stable workstream identity;
checkpoints and Git/GitHub provide evidence; native histories remain pointers.

### 2. Use a small map into versioned sources of truth

OpenAI's harness-engineering report says its large monolithic instruction file
failed because it consumed context, became stale, and resisted mechanical
verification. The replacement is a short map into structured, versioned docs,
execution plans, decision logs, and known technical debt, with linters checking
freshness and cross-links.

**Agent Bridge implication:** the HTML is an index and control surface, not the
canonical store. It links to checkpoints, repositories, issues, and Epics and
can be regenerated from those inputs.

### 3. Transfer bounded state, not conversational exhaust

The minimum useful handoff is deliberately smaller than a transcript: source
harness/session pointer, exact worktree and revision, objective and acceptance
criteria, authority limits, touched files, verification results, unresolved
findings, artifact hashes or source pointers, and freshness. The receiver must
reconcile that evidence against current Git, GitHub, and external state.

**Agent Bridge implication:** keep raw transcripts, credentials, tool dumps,
and private browser state out of the report and Git. A copied continuation
brief must instruct the receiver to re-verify drift.

### 4. Separate presence, capability, readiness, and execution

A heartbeat proves that a harness recently wrote shared state. A capability
card can prove that a command was discovered on that machine. Neither proves
that a native UI chat is idle, authentication is current, or a remote command
will succeed.

Self-described orchestration products reinforce the utility of a cross-agent
view: Orca places several CLI agents in isolated worktrees and tracks them in
one interface, including remote worktrees. That is evidence of a viable UX
pattern, not independent proof of reliability.

**Agent Bridge implication:** offer an agent only from a positive capability
card, display heartbeat freshness, and never label a copied command as a
completed dispatch.

### 5. Preserve machine-local path truth

Paths are properties of a machine and operating system. A synced checkpoint
may be portable in content while its absolute path is not. Selecting a Windows
machine must never silently reuse a macOS `/Users/...` worktree or checkpoint.

**Agent Bridge implication:** exact machine and OS matches outrank other
evidence; no cross-OS fallback is allowed. Missing evidence becomes an empty,
operator-editable field rather than an invented path. Windows commands are
explicit PowerShell, while POSIX systems use shell-safe quoting.

### 6. Distinguish exact work relationships from repository inventory

Repository membership is useful for discovery but weak evidence of semantic
relationship. Calling every issue in a repository “related” creates false
planning provenance, especially when one repository supports several
workstreams.

**Agent Bridge implication:** `repos` drives a clearly labeled repository issue
inventory. Only `issueRefs` and `epicRefs` create “Related GitHub work.” An
unresolved reference stays visible; an Epic reference must resolve to a native
Epic.

### 7. Make permissions and intervention first-class state

Cloudflare OS's Gatekeeper design is a public example of capability mediation:
it narrows resource access, logs actions, and places side effects behind human
approval. DeepSec similarly documents that a coding harness has shell-level
risk, keeps credentials outside worker sandboxes, limits egress, and resumes a
halted run after quota recovery.

These are project-authored claims and should not be generalized as proof that
the implementations are secure. They do support a design principle: permission
and intervention boundaries belong in the harness, not only in prompt prose.

**Agent Bridge implication:** report actions prepare text only. Any future
one-click dispatch needs an authenticated local consumer, explicit authority,
an audit record, idempotency, and a default-deny transport boundary.

### 8. Evaluate the model-harness-environment system, not the model alone

The harness preprint argues that completion should be judged by a verifiably
correct, attributed, maintainable change and an auditable episode. OpenAI's
account similarly emphasizes worktree-local UI, logs, metrics, traces,
mechanical architecture checks, and continuous cleanup of drift.

**Agent Bridge implication:** compare harnesses under matched model, task,
tools, permissions, budgets, and fixtures. Record versions, traces, latency,
tokens/cost, failure attribution, recovery, human intervention, and fresh
versus warmed state. Passing known fixtures is regression evidence, not proof
of novel correctness.

### 9. Keep retention explicit and distinguish history from liveness data

Long-lived task history and checkpoints are continuity evidence and should not
expire silently. Short-lived registry heartbeats are operational liveness data,
not session history. A stale heartbeat may be hidden from a default view only
when the underlying continuation evidence remains intact and a non-pruning
inspection path exists.

**Agent Bridge implication:** report generation reads the registry without
pruning. It does not delete sessions, checkpoints, tasks, or transcripts.

### 10. Validate the report as executable UI

The report's value depends on interaction correctness: default five-issue
truncation, explicit expansion, search, filters, machine/agent routing, safe
command quoting, and clear unavailable states. Dialogs, dropdowns, and toasts
must remain inside desktop and narrow viewports; accessibility and browser
errors are part of acceptance evidence.

**Agent Bridge implication:** keep the HTML self-contained and run Playwright
at representative desktop/mobile sizes with overlay-bound checks and an
accessibility scan.

## Recommended architecture

```text
private catalog + portable project registry + capability cards + GitHub
                                |
                                v
                 deterministic report generator
                                |
                                v
                self-contained local HTML report
                   |             |             |
                   v             v             v
             checkpoint      issue/Epic     copied command
              evidence          links        or handoff brief
```

The report is disposable and reproducible. Canonical state remains in project
files, checkpoints, Git, GitHub, and the local Agent Bridge ledger. Generated
HTML and raw workflow evidence remain outside the public repository.

## Open questions for the future Epic

1. What authenticated local transport, if any, may consume a resume request on
   another machine without turning a heartbeat into an RPC claim?
2. How should workstream identity map to native GitHub parent/child issue
   relationships while remaining usable across repositories?
3. Which evidence fields and freshness limits are mandatory before a control
   can change from “copy” to “queue” or “dispatch”?
4. How should matched cross-harness trials measure recovery quality, human
   attention, and failure attribution without exposing private transcripts?
5. Which retention and deletion controls require explicit operator approval on
   every supported harness?

## Sources

- OpenAI, “Harness engineering: leveraging Codex in an agent-first world,”
  February 11, 2026: https://openai.com/index/harness-engineering/
- Hailin Zhong and Shengxin Zhu, “AI Harness Engineering: A Runtime Substrate
  for Foundation-Model Software Agents,” arXiv preprint, May 13, 2026:
  https://arxiv.org/abs/2605.13357
- Agent Bridge, “Active Session Recovery”:
  https://github.com/next-citizen-llc/agent-bridge/blob/main/docs/active-session-recovery.md
- Agent Bridge, “Verifying an agent handoff without sharing private
  transcripts”:
  https://github.com/next-citizen-llc/agent-bridge/blob/main/docs/field-notes/verifiable-handoffs.md
- Cloudflare, `cloudflare-os` README, including Gatekeepers and early-access
  limitations: https://github.com/cloudflare/cloudflare-os
- Stably AI, `orca` README: https://github.com/stablyai/orca
- Vercel Labs, `deepsec` README: https://github.com/vercel-labs/deepsec
- AI Dev Daily, “Google Antigravity: A Comprehensive Guide”:
  https://aidevdaily.com/google-antigravity-a-comprehensive-guide/
- AI Dev Daily, “OpenAI Codex Review: The Best Code-Generator in 2025?”:
  https://aidevdaily.com/openai-codex-review-the-code-generation-powerhouse/

## Gaps and confidence

**Confidence: high** in the architectural boundary—durable project evidence,
explicit capability/readiness distinctions, exact path routing, and
verification-first resumption are supported by Agent Bridge's implementation
experience and multiple independent design patterns.

**Confidence: medium** in the recommended evaluation shape. The trace-based
harness paper is a preprint, and no controlled cross-harness trial from the
private research plan has yet produced results.

**Confidence: low** in comparative product claims from AI Dev Daily and project
READMEs. They are retained as discovery context only.
