"""Append-only structured trace event log for agent bridge runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

try:
    from .correlation import META_FIELDS, extract_meta, iso_now, new_id
except ImportError:
    from correlation import META_FIELDS, extract_meta, iso_now, new_id  # type: ignore[no-redef]


def state_dir() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local/state/agent-bridge")).expanduser()


def events_path() -> Path:
    return state_dir() / "events.jsonl"


def emit_event(
    event_type: str,
    *,
    run_id: str | None = None,
    data: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_meta = extract_meta(meta or {})
    if run_id:
        event_meta["run_id"] = run_id
    record: dict[str, Any] = {
        "id": new_id("evt"),
        "ts": iso_now(),
        "type": event_type,
        "data": data or {},
    }
    for field in META_FIELDS:
        if field in event_meta:
            record[field] = event_meta[field]
    traceparent = os.environ.get("AGENT_BRIDGE_TRACEPARENT")
    if traceparent:
        record["traceparent"] = traceparent
    tracestate = os.environ.get("AGENT_BRIDGE_TRACESTATE") or os.environ.get("TRACESTATE")
    if tracestate:
        record["tracestate"] = tracestate
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def load_events(*, run_id: str | None = None, event_type: str | None = None) -> list[dict[str, Any]]:
    path = events_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if run_id and record.get("run_id") != run_id:
                continue
            if event_type and record.get("type") != event_type:
                continue
            rows.append(record)
    return rows


def format_events(events: Iterable[dict[str, Any]]) -> str:
    rows = []
    for event in events:
        run = f" run={event.get('run_id')}" if event.get("run_id") else ""
        role = f" role={event.get('role')}" if event.get("role") else ""
        rows.append(f"{event['id']} {event['ts']} {event['type']}{run}{role}")
    return "\n".join(rows) if rows else "(empty)"
