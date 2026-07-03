"""Shared correlation helpers for bridge dispatch, mailbox, and loop traces."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Mapping


META_FIELDS = ("run_id", "loop_id", "turn_id", "parent_id", "attempt", "role")


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{utc_stamp()}_{uuid.uuid4().hex[:8]}"


def extract_meta(source: Mapping[str, Any] | object | None) -> dict[str, Any]:
    if source is None:
        return {}
    meta: dict[str, Any] = {}
    for field in META_FIELDS:
        if isinstance(source, Mapping):
            value = source.get(field)
        else:
            value = getattr(source, field, None)
        if value is not None and value != "":
            meta[field] = value
    return meta


def merge_meta(*sources: Mapping[str, Any] | object | None, **overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for source in sources:
        meta.update(extract_meta(source))
    for key, value in overrides.items():
        if key in META_FIELDS and value is not None and value != "":
            meta[key] = value
    return meta


def add_meta_args(parser, *, filters_only: bool = False) -> None:
    fields = ("run_id", "loop_id") if filters_only else META_FIELDS
    for field in fields:
        kwargs = {"dest": field, "default": None}
        if field == "attempt":
            kwargs["type"] = int
        parser.add_argument(f"--{field.replace('_', '-')}", **kwargs)


def ensure_run_meta(meta: Mapping[str, Any] | None = None, *, role: str | None = None) -> dict[str, Any]:
    out = extract_meta(meta or {})
    out.setdefault("run_id", new_id("run"))
    if role:
        out.setdefault("role", role)
    out.setdefault("attempt", 1)
    return out


def child_turn_meta(
    base: Mapping[str, Any],
    *,
    role: str,
    attempt: int = 1,
    parent_id: str | None = None,
) -> dict[str, Any]:
    meta = {
        key: value
        for key, value in extract_meta(base).items()
        if key not in {"turn_id", "parent_id", "role"}
    }
    meta.setdefault("run_id", new_id("run"))
    meta.setdefault("loop_id", new_id("loop"))
    meta["turn_id"] = new_id(f"turn_{role}")
    meta["role"] = role
    meta["attempt"] = attempt
    if parent_id:
        meta["parent_id"] = parent_id
    return meta


def match_meta(record: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    meta = record.get("meta") or {}
    for key, value in filters.items():
        if value is None or value == "":
            continue
        if str(meta.get(key)) != str(value):
            return False
    return True


def safe_fragment(value: Any) -> str:
    text = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)[:80] or "none"


def format_meta(meta: Mapping[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in meta.items() if key in META_FIELDS)
