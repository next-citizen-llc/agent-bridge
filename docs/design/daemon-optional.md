# Daemon-Optional Coordination (#9)

Agent Bridge coordination stays filesystem-first. A background daemon is an optional
future optimization, never a requirement: every primitive below works from one-shot CLI
invocations and survives process exit because state lives in append-only JSONL files
under the state dir (`AGENT_BRIDGE_STATE_DIR`, default `~/.local/state/agent-bridge`).

## Primitives a daemon would build on

- **Task ledger** (`agent code tasks`, `tasks.jsonl`): durable create/claim/update/
  request-input/cancel/resume/attach-artifact records. A daemon could watch this file
  to schedule work, but any harness can already read and append without one.
- **Transport** (`agent code transport`, `transport/<queue>/messages.jsonl` + `acks.jsonl`):
  delivery envelopes with dedupe keys, ack tracking, retry counters, and expiry. A daemon
  could poll queues on a timer; today `receive`/`ack` are explicit and idempotent.
- **Trust policy** (`agent code policy`, `policies.json`): allow/deny/require-approval
  rules plus optional HMAC signing. Enforcement happens at dispatch time in-process.
- **Trace envelopes** (`agent code trace --envelope`): portable, traceparent-carrying
  event export a daemon could forward, but which any tool can read directly.

## Status surface

`agent code daemon status` reports `daemon: not-running` plus the health/paths of the
primitives above. If a daemon is ever added, the same command reports its liveness; the
JSON shape (`daemon_optional: true`) is the compatibility contract, and all CLI flows
must keep working when the daemon is absent.

## Non-goals

No hosted service, no sockets, no background process installed by default, and no
behavior change for existing bridge/loop/workflow flows.
