# A failure taxonomy for cross-harness workflows

"The agent failed" is not a useful diagnosis. Cross-harness work spans several systems, and each
failure class needs a different response.

## The taxonomy

| Class | Typical evidence | Correct first response |
| --- | --- | --- |
| Target resolution | Executable missing, unsupported adapter, wrong binary path | Inspect `agent code bridge --list`, target config, and executable resolution |
| Authentication | CLI reports logged out, expired session, or provider rejection | Repair the target's own authenticated session; do not bypass it |
| Network or provider | Timeout, DNS failure, rate limit, provider error | Preserve the run ID, retry within bounds, or wait for provider state to change |
| Project context | Wrong Git root, branch, head, or unreadable source | Stop and correct `--project-dir` or checkout state |
| Authority or policy | Requested mode conflicts with read-only review or prohibited external action | Change the task or obtain explicit authority; do not weaken the boundary silently |
| Dispatch | Invalid arguments, adapter mismatch, subprocess launch failure | Inspect the resolved dry-run command and adapter construction |
| Model execution | Target process runs but returns an error, incomplete result, or no verdict | Preserve output and classify the model-level failure separately |
| Verification | Code changed but tests, build, live state, or acceptance evidence is absent | Run the requirement-matched checks; do not call the task complete |
| Handoff | Receiver lacks source path, revision, authority, or next action | Regenerate a bounded handoff manifest with missing provenance |
| State recording | Result exists but trace, finding, verdict, or workflow artifact is missing | Repair local recording without reclassifying the original task as failed |

## Why the categories matter

A fresh harness heartbeat cannot repair an expired provider login. A successful provider call
cannot prove that the correct worktree was targeted. A passing unit test cannot prove that a release
was published. Collapsing these into one green or red status encourages the wrong repair and often
destroys useful evidence.

Agent Bridge's readiness layer uses typed failures for authentication, MCP, network, configuration,
context, and source problems. The taxonomy above extends that idea through execution, verification,
handoff, and recording.

## A minimal incident record

For a failed run, preserve:

- run and turn IDs;
- target client, surface, machine, and project;
- branch and head revision;
- requested mode and hard limits;
- failure class;
- exact bounded error;
- whether any files or external state changed;
- next safe retry condition.

This lets the next operator distinguish "retry the same command" from "repair auth," "select the
correct worktree," or "do not retry without new authority."

## Operator rule

Classify first, then repair the layer that actually failed. Keep incomplete execution, missing
verification, and external acceptance as separate states.

Relevant implementation surfaces:

- [`agent_bridge/readiness.py`](../../agent_bridge/readiness.py)
- [`agent_bridge/cli.py`](../../agent_bridge/cli.py)
- [`agent_bridge/trace.py`](../../agent_bridge/trace.py)
- [`agent_bridge/findings.py`](../../agent_bridge/findings.py)
