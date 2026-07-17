"""Coordination primitives for Agent Bridge.

Stdlib-only foundations for cross-harness coordination:

- capability cards for registered harness targets (#12)
- a JSONL-backed durable task ledger (#13)
- portable trace envelopes with traceparent support (#14)
- trust policy loading, evaluation, and HMAC signing (#15)
- file/shared-folder delivery transport (#16)
- a deterministic coordination eval harness (#17)
- harness doctor drift checks (#18)
- daemon status placeholder that keeps any daemon optional (#9)

Everything here is filesystem based and backwards compatible: no daemon, no
network service, and permissive defaults for existing local review flows.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Callable

from .correlation import iso_now, new_id, safe_fragment
from .trace import emit_event, load_events


SCHEMA_VERSION = "1.0"
ENVELOPE_SCHEMA_VERSION = "1.1"
ENVELOPE_SOURCE = "agent-bridge"


def state_dir() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local/state/agent-bridge")).expanduser()


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Return (records, corrupt_line_count); tolerates corrupted lines."""
    rows: list[dict[str, Any]] = []
    corrupt = 0
    if not path.exists():
        return rows, corrupt
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows, 1
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        if isinstance(record, dict):
            rows.append(record)
        else:
            corrupt += 1
    return rows, corrupt


def _json_print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Capability cards (#12)
# ---------------------------------------------------------------------------

ADAPTER_CAPABILITIES: dict[str, dict[str, Any]] = {
    "claude_code": {
        "modes": ["review", "code"],
        "modalities": ["text", "image/png", "image/jpeg"],
        "transports": ["cli", "mcp"],
    },
    "codex_exec": {
        "modes": ["review", "code"],
        "modalities": ["text"],
        "transports": ["cli"],
    },
    "argv": {
        "modes": ["review", "code"],
        "modalities": ["text"],
        "transports": ["cli"],
    },
}


def capability_card(agent: dict[str, Any], *, bridge_dir: Path | None = None) -> dict[str, Any]:
    adapter = agent.get("adapter", "argv")
    defaults = ADAPTER_CAPABILITIES.get(adapter, ADAPTER_CAPABILITIES["argv"])
    modes = agent.get("modes") or defaults["modes"]
    if adapter == "argv" and not agent.get("modes"):
        modes = [mode for mode in ("review", "code") if agent.get(f"{mode}_args") or agent.get("args")]
        modes = modes or ["review"]
    card: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "capability_card",
        "id": agent.get("id", "unknown"),
        "label": agent.get("label", agent.get("id", "unknown")),
        "adapter": adapter,
        "command": agent.get("command", ""),
        "command_found": bool(shutil.which(str(agent.get("command", "")))),
        "modes": modes,
        "modalities": agent.get("modalities") or defaults["modalities"],
        "transports": agent.get("transports") or defaults["transports"],
        "workspace_roots": agent.get("workspace_roots") or [],
        "generated_at": iso_now(),
    }
    if bridge_dir is not None:
        mcp_path = bridge_dir / "mailbox_mcp.py"
        card["mcp"] = {
            "mailbox_path": str(mcp_path),
            "available": mcp_path.exists(),
            "auth": agent.get("mcp_auth", "local-trusted"),
        }
    return card


def explain_incompatibility(
    card: dict[str, Any],
    *,
    mode: str,
    media_suffixes: list[str] | None = None,
    project_dir: str | None = None,
) -> list[str]:
    """Return human-readable reasons a dispatch would fail; empty means compatible."""
    problems: list[str] = []
    modes = card.get("modes") or []
    if mode not in modes:
        problems.append(f"{card.get('id')}: mode {mode!r} not in supported modes {modes}")
    modalities = [str(item).lower() for item in card.get("modalities") or []]
    for suffix in media_suffixes or []:
        suffix = suffix.lower().lstrip(".")
        if suffix in {"png", "jpg", "jpeg"}:
            if not any(suffix in modality or "image" in modality for modality in modalities):
                problems.append(f"{card.get('id')}: media .{suffix} not covered by modalities {modalities}")
        elif suffix in {"heic", "heif"}:
            problems.append(f"{card.get('id')}: media .{suffix} requires bridge PNG conversion")
    roots = card.get("workspace_roots") or []
    if project_dir and roots:
        resolved = str(Path(project_dir).expanduser().resolve())
        if not any(resolved.startswith(str(Path(root).expanduser())) for root in roots):
            problems.append(f"{card.get('id')}: project {resolved} outside workspace roots {roots}")
    return problems


def capability_cards(config: dict[str, Any], *, bridge_dir: Path | None = None) -> list[dict[str, Any]]:
    return [capability_card(agent, bridge_dir=bridge_dir) for agent in config.get("agents", [])]


# ---------------------------------------------------------------------------
# Durable task ledger (#13)
# ---------------------------------------------------------------------------

TASK_EVENTS = {
    "created",
    "claimed",
    "updated",
    "input_requested",
    "cancelled",
    "resumed",
    "artifact_attached",
    "completed",
    "failed",
}

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def tasks_path() -> Path:
    return state_dir() / "tasks.jsonl"


def _task_event(task_id: str, event: str, **fields: Any) -> dict[str, Any]:
    if event not in TASK_EVENTS:
        raise ValueError(f"unknown task event {event!r}")
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": new_id("tev"),
        "task_id": task_id,
        "ts": iso_now(),
        "event": event,
    }
    record.update({key: value for key, value in fields.items() if value is not None})
    _append_jsonl(tasks_path(), record)
    return record


def task_create(
    title: str,
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    owner: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = task_id or new_id("task")
    return _task_event(task_id, "created", title=title, run_id=run_id, owner=owner, data=data)


def task_claim(task_id: str, *, owner: str, note: str | None = None) -> dict[str, Any]:
    return _task_event(task_id, "claimed", owner=owner, note=note)


def task_update(task_id: str, *, status: str | None = None, note: str | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    if status in TERMINAL_STATUSES:
        event = "cancelled" if status == "cancelled" else status
        return _task_event(task_id, event, note=note, data=data)
    return _task_event(task_id, "updated", status=status, note=note, data=data)


def task_request_input(task_id: str, *, question: str, owner: str | None = None) -> dict[str, Any]:
    return _task_event(task_id, "input_requested", question=question, owner=owner)


def task_cancel(task_id: str, *, reason: str | None = None) -> dict[str, Any]:
    return _task_event(task_id, "cancelled", note=reason)


def task_resume(task_id: str, *, note: str | None = None) -> dict[str, Any]:
    return _task_event(task_id, "resumed", note=note)


def task_attach_artifact(task_id: str, *, path: str, kind: str = "file", note: str | None = None) -> dict[str, Any]:
    return _task_event(task_id, "artifact_attached", artifact={"path": path, "kind": kind}, note=note)


def task_inspect(task_id: str) -> dict[str, Any]:
    rows, corrupt = _read_jsonl(tasks_path())
    events = [row for row in rows if row.get("task_id") == task_id]
    status = "unknown"
    title = ""
    owner = None
    artifacts: list[dict[str, Any]] = []
    pending_input: list[str] = []
    run_id = None
    for row in events:
        event = row.get("event")
        if event == "created":
            status = "open"
            title = row.get("title", "")
            run_id = row.get("run_id") or run_id
            owner = row.get("owner") or owner
        elif event == "claimed":
            status = "claimed"
            owner = row.get("owner") or owner
        elif event == "updated":
            status = row.get("status") or status
        elif event == "input_requested":
            status = "needs_input"
            pending_input.append(str(row.get("question", "")))
        elif event == "resumed":
            status = "claimed" if owner else "open"
            pending_input = []
        elif event == "artifact_attached":
            artifact = row.get("artifact")
            if isinstance(artifact, dict):
                artifacts.append(artifact)
        elif event in {"completed", "failed", "cancelled"}:
            status = event
    return {
        "task_id": task_id,
        "title": title,
        "status": status,
        "owner": owner,
        "run_id": run_id,
        "pending_input": pending_input,
        "artifacts": artifacts,
        "events": events,
        "corrupt_lines": corrupt,
    }


def task_list(*, status: str | None = None) -> list[dict[str, Any]]:
    rows, _ = _read_jsonl(tasks_path())
    seen: list[str] = []
    for row in rows:
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id not in seen:
            seen.append(task_id)
    summaries = []
    for task_id in seen:
        summary = task_inspect(task_id)
        summary.pop("events", None)
        if status and summary["status"] != status:
            continue
        summaries.append(summary)
    return summaries


def record_run_task(
    event: str,
    *,
    meta: dict[str, Any],
    command: str,
    data: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Best-effort task ledger hook for bridge/loop/workflow runs."""
    try:
        run_id = str(meta.get("run_id") or "")
        if not run_id:
            return None
        task_id = f"task_{safe_fragment(run_id)}"
        if event == "created":
            return task_create(f"{command} run {run_id}", task_id=task_id, run_id=run_id, owner=str(meta.get("role") or "") or None, data=data)
        return _task_event(task_id, event, data=data, artifact=artifact)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Trace envelopes (#14)
# ---------------------------------------------------------------------------

def _stable_hex(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def make_traceparent(run_id: str, span_seed: str | None = None) -> str:
    trace_id = _stable_hex(f"trace:{run_id}", 32)
    span_id = _stable_hex(f"span:{span_seed or run_id}", 16)
    return f"00-{trace_id}-{span_id}-01"


def parse_traceparent(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    return {"version": parts[0], "trace_id": parts[1], "span_id": parts[2], "flags": parts[3]}


def current_trace_context(meta: dict[str, Any] | None = None) -> dict[str, str]:
    """Resolve trace context from the environment or run metadata."""
    inherited = parse_traceparent(os.environ.get("AGENT_BRIDGE_TRACEPARENT") or os.environ.get("TRACEPARENT"))
    meta = meta or {}
    run_id = str(meta.get("run_id") or "local")
    if inherited:
        traceparent = f"00-{inherited['trace_id']}-{_stable_hex('span:' + str(meta.get('turn_id') or run_id), 16)}-01"
    else:
        traceparent = make_traceparent(run_id, span_seed=str(meta.get("turn_id") or run_id))
    context = {"traceparent": traceparent}
    tracestate = os.environ.get("AGENT_BRIDGE_TRACESTATE") or os.environ.get("TRACESTATE")
    if tracestate:
        context["tracestate"] = tracestate
    return context


def set_trace_context(meta: dict[str, Any] | None = None) -> dict[str, str]:
    """Export trace context to the environment so child dispatches inherit it."""
    context = current_trace_context(meta)
    os.environ["AGENT_BRIDGE_TRACEPARENT"] = context["traceparent"]
    return context


def event_envelope(record: dict[str, Any]) -> dict[str, Any]:
    run_id = record.get("run_id")
    envelope = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "id": record.get("id", new_id("evt")),
        "time": record.get("ts", iso_now()),
        "type": record.get("type", "unknown"),
        "source": ENVELOPE_SOURCE,
        "subject": run_id or record.get("turn_id") or "",
        "traceparent": record.get("traceparent") or make_traceparent(str(run_id or record.get("id", "local"))),
        "data": record.get("data", {}),
    }
    if record.get("tracestate"):
        envelope["tracestate"] = record["tracestate"]
    meta = {key: record[key] for key in ("run_id", "loop_id", "turn_id", "parent_id", "attempt", "role") if key in record}
    if meta:
        envelope["meta"] = meta
    return envelope


def export_envelopes(*, run_id: str | None = None, event_type: str | None = None) -> list[dict[str, Any]]:
    return [event_envelope(row) for row in load_events(run_id=run_id, event_type=event_type)]


# ---------------------------------------------------------------------------
# Trust policy (#15)
# ---------------------------------------------------------------------------

POLICY_FIELDS = ("client", "machine", "repo", "mode", "action", "tool_class")


def policies_path() -> Path:
    override = os.environ.get("AGENT_BRIDGE_POLICIES")
    return Path(override).expanduser() if override else state_dir() / "policies.json"


def load_policies(path: Path | None = None) -> dict[str, Any]:
    path = path or policies_path()
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "default": "allow", "rules": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "default": "allow", "rules": [], "load_error": True}
    if not isinstance(data, dict):
        return {"schema_version": SCHEMA_VERSION, "default": "allow", "rules": [], "load_error": True}
    data.setdefault("default", "allow")
    if not isinstance(data.get("rules"), list):
        data["rules"] = []
    return data


def _rule_matches(rule: dict[str, Any], request: dict[str, Any]) -> bool:
    for field in POLICY_FIELDS:
        expected = rule.get(field)
        if expected in (None, "", "*"):
            continue
        actual = str(request.get(field, "") or "")
        values = expected if isinstance(expected, list) else [expected]
        if not any(str(value) == actual or str(value) == "*" for value in values):
            return False
    return True


def evaluate_policy(request: dict[str, Any], *, policies: dict[str, Any] | None = None, trace: bool = True) -> dict[str, Any]:
    """Return {decision, reason, rule}. Default is allow (permissive local flows)."""
    policies = policies or load_policies()
    decision = str(policies.get("default", "allow"))
    reason = "default policy"
    matched: dict[str, Any] | None = None
    for rule in policies.get("rules", []):
        if isinstance(rule, dict) and _rule_matches(rule, request):
            decision = str(rule.get("decision", "allow"))
            reason = str(rule.get("reason", "matched rule"))
            matched = rule
            break
    if decision not in {"allow", "deny", "require_approval"}:
        decision = "allow"
        reason = f"unknown decision normalized to allow ({reason})"
    result = {"decision": decision, "reason": reason, "rule": matched, "request": request}
    if trace and (matched is not None or decision != "allow"):
        try:
            emit_event("policy.decision", run_id=request.get("run_id"), data={"decision": decision, "reason": reason, "request": {k: v for k, v in request.items() if k in POLICY_FIELDS}})
        except OSError:
            pass
    return result


def _hmac_key(policies: dict[str, Any] | None = None) -> bytes | None:
    env_key = os.environ.get("AGENT_BRIDGE_HMAC_KEY")
    if env_key:
        return env_key.encode("utf-8")
    policies = policies or load_policies()
    key_file = policies.get("hmac_key_file")
    if key_file:
        try:
            return Path(key_file).expanduser().read_bytes().strip()
        except OSError:
            return None
    return None


def sign_payload(payload: dict[str, Any], *, key: bytes | None = None) -> str | None:
    key = key or _hmac_key()
    if not key:
        return None
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_payload(payload: dict[str, Any], signature: str | None, *, key: bytes | None = None) -> dict[str, Any]:
    key = key or _hmac_key()
    if not key:
        return {"verified": None, "reason": "no HMAC key configured; verification skipped"}
    if not signature:
        return {"verified": False, "reason": "message is unsigned but an HMAC key is configured"}
    expected = sign_payload(payload, key=key)
    if expected and hmac.compare_digest(expected, signature):
        return {"verified": True, "reason": "signature valid"}
    return {"verified": False, "reason": "signature mismatch"}


# ---------------------------------------------------------------------------
# File/shared-folder transport (#16)
# ---------------------------------------------------------------------------

def transport_dir(root: str | None = None) -> Path:
    if root:
        return Path(root).expanduser()
    return state_dir() / "transport"


def _queue_paths(queue: str, root: str | None = None) -> tuple[Path, Path]:
    base = transport_dir(root) / safe_fragment(queue)
    return base / "messages.jsonl", base / "acks.jsonl"


def make_delivery(
    payload: dict[str, Any],
    *,
    source: str,
    message_type: str,
    dedupe_key: str | None = None,
    ack_required: bool = True,
    max_retries: int = 3,
    expires_at: str | None = None,
    sign: bool = False,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "delivery",
        "id": new_id("msg"),
        "time": iso_now(),
        "source": source,
        "type": message_type,
        "dedupe_key": dedupe_key or _stable_hex(json.dumps(payload, sort_keys=True) + message_type, 24),
        "ack": {"required": ack_required},
        "retry": {"count": 0, "max": max_retries},
        "expires_at": expires_at,
        "error": None,
        "data": payload,
    }
    envelope.update(current_trace_context())
    if sign:
        signature = sign_payload(payload)
        if signature:
            envelope["signature"] = signature
    return envelope


def transport_send(queue: str, envelope: dict[str, Any], *, root: str | None = None) -> dict[str, Any]:
    messages_path, _ = _queue_paths(queue, root)
    existing, _ = _read_jsonl(messages_path)
    dedupe_key = envelope.get("dedupe_key")
    for row in existing:
        if dedupe_key and row.get("dedupe_key") == dedupe_key:
            return {"status": "duplicate", "id": row.get("id"), "dedupe_key": dedupe_key}
    _append_jsonl(messages_path, envelope)
    return {"status": "sent", "id": envelope["id"], "dedupe_key": dedupe_key}


def transport_ack(queue: str, message_id: str, *, root: str | None = None, error: str | None = None) -> dict[str, Any]:
    _, acks_path = _queue_paths(queue, root)
    record = {"id": new_id("ack"), "message_id": message_id, "ts": iso_now(), "error": error}
    _append_jsonl(acks_path, record)
    return record


def _is_expired(envelope: dict[str, Any]) -> bool:
    expires_at = envelope.get("expires_at")
    return bool(expires_at and str(expires_at) <= iso_now())


def transport_receive(queue: str, *, root: str | None = None, include_acked: bool = False) -> dict[str, Any]:
    messages_path, acks_path = _queue_paths(queue, root)
    messages, corrupt_messages = _read_jsonl(messages_path)
    acks, corrupt_acks = _read_jsonl(acks_path)
    acked = {row.get("message_id") for row in acks}
    pending: list[dict[str, Any]] = []
    expired: list[str] = []
    seen_dedupe: set[str] = set()
    for row in messages:
        dedupe_key = str(row.get("dedupe_key") or row.get("id") or "")
        if dedupe_key in seen_dedupe:
            continue
        seen_dedupe.add(dedupe_key)
        if _is_expired(row):
            expired.append(str(row.get("id")))
            continue
        if not include_acked and row.get("id") in acked:
            continue
        pending.append(row)
    return {
        "queue": queue,
        "pending": pending,
        "expired": expired,
        "acked_count": len(acked),
        "corrupt_lines": corrupt_messages + corrupt_acks,
    }


def transport_status(*, root: str | None = None) -> dict[str, Any]:
    base = transport_dir(root)
    queues: list[dict[str, Any]] = []
    if base.exists():
        for entry in sorted(base.iterdir()):
            if entry.is_dir():
                state = transport_receive(entry.name, root=str(base))
                queues.append(
                    {
                        "queue": entry.name,
                        "pending": len(state["pending"]),
                        "expired": len(state["expired"]),
                        "acked": state["acked_count"],
                        "corrupt_lines": state["corrupt_lines"],
                    }
                )
    return {"transport_dir": str(base), "exists": base.exists(), "queues": queues}


def transport_smoke(*, root: str | None = None) -> dict[str, Any]:
    queue = "smoke"
    envelope = make_delivery({"check": "transport"}, source="smoke", message_type="transport.smoke", dedupe_key=f"smoke-{new_id('sm')}")
    sent = transport_send(queue, envelope, root=root)
    received = transport_receive(queue, root=root)
    found = any(row.get("id") == envelope["id"] for row in received["pending"])
    transport_ack(queue, envelope["id"], root=root)
    after = transport_receive(queue, root=root)
    still_pending = any(row.get("id") == envelope["id"] for row in after["pending"])
    ok = sent["status"] == "sent" and found and not still_pending
    return {"ok": ok, "sent": sent, "delivered": found, "acked": not still_pending, "corrupt_lines": after["corrupt_lines"]}


# ---------------------------------------------------------------------------
# Deterministic coordination eval harness (#17)
# ---------------------------------------------------------------------------

def _scenario_handoff_success(root: str) -> dict[str, Any]:
    record = task_create("handoff: implement fix", owner="builder")
    task_id = record["task_id"]
    task_claim(task_id, owner="critic")
    task_attach_artifact(task_id, path="artifacts/fix.diff")
    task_update(task_id, status="completed")
    status = task_inspect(task_id)
    passed = status["status"] == "completed" and len(status["artifacts"]) == 1
    return {"passed": passed, "detail": f"final status {status['status']} with {len(status['artifacts'])} artifact(s)"}


def _scenario_stale_target(root: str) -> dict[str, Any]:
    card = capability_card({"id": "stale", "adapter": "argv", "command": "definitely-not-a-real-cli", "modes": ["review"]})
    problems = explain_incompatibility(card, mode="code")
    passed = bool(problems) and not card["command_found"]
    return {"passed": passed, "detail": problems[0] if problems else "stale target was not detected"}


def _scenario_budget_failure(root: str) -> dict[str, Any]:
    record = task_create("budget-capped run")
    task_update(record["task_id"], status="failed", note="Exceeded USD budget")
    status = task_inspect(record["task_id"])
    passed = status["status"] == "failed"
    return {"passed": passed, "detail": f"budget failure recorded as {status['status']}"}


def _scenario_auth_failure(root: str) -> dict[str, Any]:
    decision = evaluate_policy(
        {"client": "unknown-remote", "action": "dispatch", "mode": "code"},
        policies={"default": "allow", "rules": [{"client": "unknown-remote", "decision": "deny", "reason": "unauthenticated client"}]},
        trace=False,
    )
    return {"passed": decision["decision"] == "deny", "detail": decision["reason"]}


def _scenario_cancellation(root: str) -> dict[str, Any]:
    record = task_create("cancellable work")
    task_cancel(record["task_id"], reason="operator cancelled")
    resumed = task_resume(record["task_id"], note="operator resumed")
    status = task_inspect(record["task_id"])
    passed = status["status"] in {"open", "claimed"} and bool(resumed)
    return {"passed": passed, "detail": f"post-resume status {status['status']}"}


def _scenario_duplicate_delivery(root: str) -> dict[str, Any]:
    envelope = make_delivery({"n": 1}, source="eval", message_type="eval.dup", dedupe_key="dup-1")
    first = transport_send("eval", envelope, root=root)
    second = transport_send("eval", dict(envelope, id=new_id("msg")), root=root)
    pending = transport_receive("eval", root=root)["pending"]
    count = sum(1 for row in pending if row.get("dedupe_key") == "dup-1")
    passed = first["status"] == "sent" and second["status"] == "duplicate" and count == 1
    return {"passed": passed, "detail": f"second send {second['status']}, {count} pending copy"}


def _scenario_unsigned_message(root: str) -> dict[str, Any]:
    key = b"eval-key"
    verdict = verify_payload({"cmd": "rm -rf"}, None, key=key)
    tampered = sign_payload({"cmd": "ls"}, key=key)
    forged = verify_payload({"cmd": "rm -rf"}, tampered, key=key)
    passed = verdict["verified"] is False and forged["verified"] is False
    return {"passed": passed, "detail": f"unsigned: {verdict['reason']}; tampered: {forged['reason']}"}


def _scenario_conflicting_verdicts(root: str) -> dict[str, Any]:
    record = task_create("conflicting verdicts")
    task_id = record["task_id"]
    task_update(task_id, status="approved", note="critic A approves")
    task_update(task_id, status="rejected", note="critic B rejects")
    task_request_input(task_id, question="Verdicts conflict; which critic wins?")
    task_resume(task_id, note="operator picked critic B")
    status = task_inspect(record["task_id"])
    passed = status["status"] != "needs_input" and not status["pending_input"]
    return {"passed": passed, "detail": f"resolved to {status['status']} after resume"}


EVAL_SCENARIOS: list[tuple[str, Callable[[str], dict[str, Any]]]] = [
    ("handoff_success", _scenario_handoff_success),
    ("stale_target", _scenario_stale_target),
    ("budget_failure", _scenario_budget_failure),
    ("auth_failure", _scenario_auth_failure),
    ("cancellation_resume", _scenario_cancellation),
    ("duplicate_delivery", _scenario_duplicate_delivery),
    ("malicious_unsigned_message", _scenario_unsigned_message),
    ("conflicting_verdicts", _scenario_conflicting_verdicts),
]


def run_coordination_eval(*, transport_root: str | None = None) -> dict[str, Any]:
    root = transport_root or str(state_dir() / "eval-transport")
    results: list[dict[str, Any]] = []
    for name, scenario in EVAL_SCENARIOS:
        try:
            outcome = scenario(root)
        except Exception as exc:  # deterministic harness: a crash is a scenario failure
            outcome = {"passed": False, "detail": f"scenario raised {exc!r}"}
        results.append({"scenario": name, **outcome})
    passed = sum(1 for row in results if row["passed"])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


def format_eval_markdown(scorecard: dict[str, Any]) -> str:
    lines = [
        "# Coordination Eval Scorecard",
        "",
        f"Generated: {scorecard['generated_at']}",
        f"Passed: {scorecard['passed']}/{scorecard['total']}",
        "",
        "| Scenario | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for row in scorecard["results"]:
        result = "pass" if row["passed"] else "FAIL"
        lines.append(f"| {row['scenario']} | {result} | {row['detail']} |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Harness doctor (#18)
# ---------------------------------------------------------------------------

def run_doctor(
    *,
    skill_text: str | None = None,
    shared_root: Path | None = None,
    bridge_dir: Path | None = None,
    config_loader: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool | None, detail: str) -> None:
        status = "skip" if ok is None else ("ok" if ok else "fail")
        checks.append({"check": name, "status": status, "detail": detail})

    sdir = state_dir()
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        probe = sdir / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("state_dir_writable", True, str(sdir))
    except OSError as exc:
        check("state_dir_writable", False, f"{sdir}: {exc}")

    if config_loader is not None:
        try:
            config_loader()
            check("agents_config", True, "agents.json loads and validates")
        except Exception as exc:
            check("agents_config", False, str(exc))

    if bridge_dir is not None:
        mcp = bridge_dir / "mailbox_mcp.py"
        check("mailbox_mcp_path", mcp.exists(), str(mcp))

    if shared_root is None:
        check("shared_skill", None, "no shared skills root found; skipping skill drift checks")
    else:
        skill_dir = shared_root / "Agent-Bridge"
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            check("shared_skill_installed", False, f"missing {skill_md}")
        else:
            check("shared_skill_installed", True, str(skill_md))
            if skill_text is not None:
                installed = skill_md.read_text(encoding="utf-8")
                if installed.strip() == skill_text.strip():
                    check("shared_skill_fresh", True, "installed skill matches generated text")
                else:
                    check("shared_skill_fresh", False, "installed skill text drifted; rerun `agent code harness install-skill`")
        for client_dir in (Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills"):
            link = client_dir / "agent-bridge"
            if not link.exists() and not link.is_symlink():
                check(f"skill_link:{client_dir.parent.name}", None, f"{link} not present")
                continue
            if link.is_symlink():
                target = link.resolve() if link.exists() else None
                ok = target is not None and str(target).startswith(str(skill_dir.resolve() if skill_dir.exists() else skill_dir))
                check(f"skill_link:{client_dir.parent.name}", ok, f"{link} -> {target or 'broken'}")
            else:
                check(f"skill_link:{client_dir.parent.name}", False, f"{link} exists but is not a symlink")

    hook_configs = (
        (".claude", (Path.home() / ".claude" / "settings.json",)),
        (".codex", (Path.home() / ".codex" / "hooks.json", Path.home() / ".codex" / "config.toml")),
        (".grok", (Path.home() / ".grok" / "hooks" / "agent-bridge.json", Path.home() / ".grok" / "config.toml")),
    )
    for name, candidates in hook_configs:
        present = [path for path in candidates if path.exists()]
        if not present:
            check(f"hook_config:{name}", None, f"{candidates[0]} not present")
            continue
        ok = False
        detail = "no session-start hook command found"
        for settings_path in present:
            try:
                text = settings_path.read_text(encoding="utf-8")
            except OSError as exc:
                detail = str(exc)
                continue
            if "session-start" in text or "sessionstart" in text.lower():
                ok = True
                detail = f"session-start hook command found ({settings_path})"
                break
        check(f"hook_config:{name}", ok, detail)

    failures = sum(1 for row in checks if row["status"] == "fail")
    return {"schema_version": SCHEMA_VERSION, "generated_at": iso_now(), "ok": failures == 0, "failures": failures, "checks": checks}


def format_doctor(report: dict[str, Any]) -> str:
    summary = "ok" if report["ok"] else "{0} failure(s)".format(report["failures"])
    lines = [f"Harness doctor: {summary}"]
    for row in report["checks"]:
        lines.append(f"[{row['status']:>4}] {row['check']}: {row['detail']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Daemon status placeholder (#9)
# ---------------------------------------------------------------------------

def daemon_status() -> dict[str, Any]:
    """A daemon stays optional: primitives below are filesystem based."""
    return {
        "schema_version": SCHEMA_VERSION,
        "daemon": "not-running",
        "daemon_optional": True,
        "design_doc": "docs/design/daemon-optional.md",
        "primitives": {
            "task_ledger": {"path": str(tasks_path()), "exists": tasks_path().exists()},
            "transport": transport_status(),
            "policies": {"path": str(policies_path()), "exists": policies_path().exists()},
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def coord_cmd(command: str, argv: list[str]) -> int:
    if command == "capabilities":
        return _capabilities_cmd(argv)
    if command == "tasks":
        return _tasks_cmd(argv)
    if command == "transport":
        return _transport_cmd(argv)
    if command == "policy":
        return _policy_cmd(argv)
    if command == "eval":
        return _eval_cmd(argv)
    if command == "daemon":
        return _daemon_cmd(argv)
    raise ValueError(f"unknown coordination command {command!r}")


def _capabilities_cmd(argv: list[str]) -> int:
    from .cli import BRIDGE_DIR, agent_map, load_config, DEFAULT_CONFIG

    parser = argparse.ArgumentParser(prog="agent code capabilities", description="Emit portable capability cards for configured harness targets.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--to", dest="target", help="Only emit the card for this agent id")
    parser.add_argument("--check-mode", help="Explain incompatibilities for this mode instead of emitting cards")
    args = parser.parse_args(argv)
    config = load_config(Path(args.config))
    cards = capability_cards(config, bridge_dir=BRIDGE_DIR)
    if args.target:
        cards = [card for card in cards if card["id"] == args.target]
        if not cards:
            print(f"agent: no configured agent with id {args.target!r}", file=sys.stderr)
            return 2
    if args.check_mode:
        rc = 0
        for card in cards:
            problems = explain_incompatibility(card, mode=args.check_mode)
            if problems:
                rc = 1
                for problem in problems:
                    print(problem)
            else:
                print(f"{card['id']}: compatible with mode {args.check_mode!r}")
        return rc
    _json_print(cards if len(cards) != 1 else cards[0])
    return 0


def _tasks_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code tasks", description="Durable JSONL task ledger.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--run-id")
    create.add_argument("--owner")
    for name in ("claim", "update", "request-input", "cancel", "resume", "attach", "inspect"):
        p = sub.add_parser(name)
        p.add_argument("--task-id", required=True)
        if name == "claim":
            p.add_argument("--owner", required=True)
        if name == "update":
            p.add_argument("--status")
            p.add_argument("--note")
        if name == "request-input":
            p.add_argument("--question", required=True)
        if name == "cancel":
            p.add_argument("--reason")
        if name == "resume":
            p.add_argument("--note")
        if name == "attach":
            p.add_argument("--path", required=True)
            p.add_argument("--kind", default="file")
    list_parser = sub.add_parser("list")
    list_parser.add_argument("--status")
    args = parser.parse_args(argv)
    if args.cmd == "create":
        _json_print(task_create(args.title, run_id=args.run_id, owner=args.owner))
    elif args.cmd == "claim":
        _json_print(task_claim(args.task_id, owner=args.owner))
    elif args.cmd == "update":
        _json_print(task_update(args.task_id, status=args.status, note=args.note))
    elif args.cmd == "request-input":
        _json_print(task_request_input(args.task_id, question=args.question))
    elif args.cmd == "cancel":
        _json_print(task_cancel(args.task_id, reason=args.reason))
    elif args.cmd == "resume":
        _json_print(task_resume(args.task_id, note=args.note))
    elif args.cmd == "attach":
        _json_print(task_attach_artifact(args.task_id, path=args.path, kind=args.kind))
    elif args.cmd == "inspect":
        _json_print(task_inspect(args.task_id))
    else:
        _json_print(task_list(status=args.status))
    return 0


def _transport_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code transport", description="File/shared-folder message transport.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    send = sub.add_parser("send")
    send.add_argument("--queue", required=True)
    send.add_argument("--source", default="human")
    send.add_argument("--type", dest="message_type", default="message")
    send.add_argument("--payload", default="{}", help="JSON payload")
    send.add_argument("--dedupe-key")
    send.add_argument("--sign", action="store_true")
    send.add_argument("--dir", dest="root")
    receive = sub.add_parser("receive")
    receive.add_argument("--queue", required=True)
    receive.add_argument("--dir", dest="root")
    ack = sub.add_parser("ack")
    ack.add_argument("--queue", required=True)
    ack.add_argument("--id", required=True)
    ack.add_argument("--dir", dest="root")
    status = sub.add_parser("status")
    status.add_argument("--dir", dest="root")
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--dir", dest="root")
    args = parser.parse_args(argv)
    if args.cmd == "send":
        payload = json.loads(args.payload)
        envelope = make_delivery(payload, source=args.source, message_type=args.message_type, dedupe_key=args.dedupe_key, sign=args.sign)
        _json_print(transport_send(args.queue, envelope, root=args.root))
        return 0
    if args.cmd == "receive":
        _json_print(transport_receive(args.queue, root=args.root))
        return 0
    if args.cmd == "ack":
        _json_print(transport_ack(args.queue, args.id, root=args.root))
        return 0
    if args.cmd == "smoke":
        result = transport_smoke(root=args.root)
        _json_print(result)
        return 0 if result["ok"] else 1
    _json_print(transport_status(root=args.root))
    return 0


def _policy_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code policy", description="Local trust policy checks and message signing.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    for field in POLICY_FIELDS:
        check.add_argument(f"--{field.replace('_', '-')}")
    sub.add_parser("show")
    sign = sub.add_parser("sign")
    sign.add_argument("--payload", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--payload", required=True)
    verify.add_argument("--signature")
    args = parser.parse_args(argv)
    if args.cmd == "show":
        _json_print(load_policies())
        return 0
    if args.cmd == "sign":
        signature = sign_payload(json.loads(args.payload))
        _json_print({"signature": signature, "signed": signature is not None})
        return 0 if signature else 1
    if args.cmd == "verify":
        result = verify_payload(json.loads(args.payload), args.signature)
        _json_print(result)
        return 0 if result["verified"] is not False else 1
    request = {field: getattr(args, field) for field in POLICY_FIELDS}
    result = evaluate_policy(request)
    _json_print({key: result[key] for key in ("decision", "reason")})
    return 0 if result["decision"] == "allow" else 1


def _eval_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code eval", description="Deterministic coordination eval scorecard.")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--out", help="Directory to write scorecard.json and scorecard.md")
    args = parser.parse_args(argv)
    scorecard = run_coordination_eval()
    if args.out:
        out = Path(args.out).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        (out / "scorecard.json").write_text(json.dumps(scorecard, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out / "scorecard.md").write_text(format_eval_markdown(scorecard), encoding="utf-8")
    if args.markdown:
        print(format_eval_markdown(scorecard), end="")
    else:
        _json_print(scorecard)
    return 0 if scorecard["failed"] == 0 else 1


def _daemon_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code daemon", description="Daemon status placeholder; coordination stays daemon-optional.")
    parser.add_argument("cmd", choices=["status"])
    parser.parse_args(argv)
    _json_print(daemon_status())
    return 0
