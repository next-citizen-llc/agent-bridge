"""Pure, idempotent adapters from a canonical context manifest to harness files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


BEGIN_MARKER = "<!-- BEGIN agent-bridge generated context -->"
END_MARKER = "<!-- END agent-bridge generated context -->"
SCHEMA_VERSION = "1.0"
DEFAULT_PRECEDENCE = ["canonical_modules", "harness_adapter", "manual_outside_generated_region"]


class ContextAdapterError(RuntimeError):
    pass


def load_context_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContextAdapterError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data.get("modules"), list) or not isinstance(data.get("adapters"), list):
        raise ContextAdapterError(f"{path} must define modules and adapters lists")
    precedence = data.get("precedence", DEFAULT_PRECEDENCE)
    if not isinstance(precedence, list) or not all(isinstance(row, str) and row for row in precedence):
        raise ContextAdapterError(f"{path} precedence must be a list of non-empty strings")
    foreign_sources = data.get("foreign_sources", [])
    if not isinstance(foreign_sources, list) or not all(isinstance(row, str) and row for row in foreign_sources):
        raise ContextAdapterError(f"{path} foreign_sources must be a list of paths")
    data["precedence"] = precedence
    data["foreign_sources"] = foreign_sources
    return data


def _module_map(manifest: dict[str, Any], *, manifest_path: Path) -> dict[str, dict[str, Any]]:
    modules: dict[str, dict[str, Any]] = {}
    for raw in manifest["modules"]:
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("path"):
            raise ContextAdapterError("every context module must define id and path")
        module_id = str(raw["id"])
        if module_id in modules:
            raise ContextAdapterError(f"duplicate context module {module_id!r}")
        source = Path(str(raw["path"])).expanduser()
        if not source.is_absolute():
            source = manifest_path.parent / source
        if not source.exists():
            if raw.get("required", True):
                raise ContextAdapterError(f"required context module is missing: {source}")
            text = ""
        else:
            text = source.read_text(encoding="utf-8").strip()
        modules[module_id] = {"id": module_id, "source": source, "text": text}
    return modules


def render_adapter(manifest: dict[str, Any], adapter: dict[str, Any], *, manifest_path: Path) -> str:
    modules = _module_map(manifest, manifest_path=manifest_path)
    requested = adapter.get("modules", [])
    if not isinstance(requested, list) or not requested:
        raise ContextAdapterError(f"adapter {adapter.get('client', 'unknown')!r} must select at least one module")
    sections: list[str] = []
    digest = hashlib.sha256()
    for module_id in requested:
        if module_id not in modules:
            raise ContextAdapterError(f"adapter references unknown module {module_id!r}")
        module = modules[module_id]
        digest.update(str(module_id).encode("utf-8"))
        digest.update(b"\0")
        digest.update(module["text"].encode("utf-8"))
        title = str(module_id).replace("-", " ").replace("_", " ").title()
        sections.append(f"## {title}\n\n{module['text']}".rstrip())
    body = "\n\n".join(sections)
    return (
        f"{BEGIN_MARKER}\n"
        f"<!-- source-digest: sha256:{digest.hexdigest()} -->\n"
        f"{body}\n"
        f"{END_MARKER}"
    )


def canonical_hash(manifest: dict[str, Any], *, manifest_path: Path) -> str:
    modules = _module_map(manifest, manifest_path=manifest_path)
    digest = hashlib.sha256()
    for module_id in sorted(modules):
        digest.update(module_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(modules[module_id]["text"].encode("utf-8"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def merge_generated_block(existing: str, generated: str) -> str:
    begin_count = existing.count(BEGIN_MARKER)
    end_count = existing.count(END_MARKER)
    if begin_count != end_count or begin_count > 1:
        raise ContextAdapterError("adapter has incomplete or duplicate generated-context markers")
    if begin_count == 0:
        prefix = existing.rstrip()
        return f"{prefix}\n\n{generated}\n" if prefix else f"{generated}\n"
    start = existing.index(BEGIN_MARKER)
    if existing.index(END_MARKER) < start:
        raise ContextAdapterError("adapter generated-context markers are reversed")
    end = existing.index(END_MARKER, start) + len(END_MARKER)
    return existing[:start] + generated + existing[end:]


def _outside_generated_block(existing: str) -> str:
    if BEGIN_MARKER not in existing:
        return existing
    if END_MARKER not in existing:
        return existing
    start = existing.index(BEGIN_MARKER)
    if existing.index(END_MARKER) < start:
        return existing
    end = existing.index(END_MARKER, start) + len(END_MARKER)
    return existing[:start] + existing[end:]


def context_overlaps(manifest: dict[str, Any], *, manifest_path: Path) -> list[dict[str, str]]:
    """Find canonical text duplicated outside generated regions or in declared foreign sources."""
    modules = _module_map(manifest, manifest_path=manifest_path)
    sources: list[tuple[str, Path, str]] = []
    for adapter in manifest["adapters"]:
        if not isinstance(adapter, dict) or not adapter.get("path"):
            continue
        target = Path(str(adapter["path"])).expanduser()
        if not target.is_absolute():
            target = manifest_path.parent / target
        try:
            text = _outside_generated_block(target.read_text(encoding="utf-8"))
        except OSError:
            text = ""
        sources.append((f"adapter:{adapter.get('client', 'unknown')}", target, text))
    for raw in manifest.get("foreign_sources", []):
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = manifest_path.parent / target
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            text = ""
        sources.append(("foreign", target, text))
    overlaps: list[dict[str, str]] = []
    for module_id, module in modules.items():
        module_text = module["text"].strip()
        if len(module_text) < 12:
            continue
        for source_type, path, text in sources:
            if module_text in text:
                overlaps.append({"module": module_id, "source": source_type, "path": str(path)})
    return overlaps


def context_status(manifest_path: Path, *, client: str = "") -> dict[str, Any]:
    manifest = load_context_manifest(manifest_path)
    overlaps = context_overlaps(manifest, manifest_path=manifest_path)
    rows: list[dict[str, Any]] = []
    for adapter in manifest["adapters"]:
        if not isinstance(adapter, dict) or not adapter.get("client") or not adapter.get("path"):
            raise ContextAdapterError("every context adapter must define client and path")
        if client and adapter["client"] != client:
            continue
        target = Path(str(adapter["path"])).expanduser()
        if not target.is_absolute():
            target = manifest_path.parent / target
        expected = render_adapter(manifest, adapter, manifest_path=manifest_path)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        try:
            merged = merge_generated_block(existing, expected)
            if not target.exists():
                status = "missing"
            elif existing == merged:
                status = "current"
            else:
                status = "stale"
            error = ""
        except ContextAdapterError as exc:
            status = "conflict"
            error = str(exc)
        rows.append(
            {
                "client": adapter["client"],
                "path": str(target),
                "status": status,
                "error_class": "context_stale" if status in {"missing", "stale", "conflict"} else "",
                "error": error,
            }
        )
    # A manifest with `retired_on` set and no adapters is a deliberate end state
    # (for example an expired announcement), not a misconfiguration.
    retired_on = str(manifest.get("retired_on") or "")
    retired = bool(retired_on) and not manifest["adapters"]
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest": str(manifest_path),
        "canonical_hash": canonical_hash(manifest, manifest_path=manifest_path),
        "precedence": manifest["precedence"],
        "overlaps": overlaps,
        "retired_on": retired_on,
        "ok": retired or (bool(rows) and all(row["status"] == "current" for row in rows) and not overlaps),
        "adapters": rows,
    }


def install_context_adapters(manifest_path: Path, *, client: str = "", check: bool = False, force: bool = False) -> dict[str, Any]:
    manifest = load_context_manifest(manifest_path)
    rows: list[dict[str, Any]] = []
    for adapter in manifest["adapters"]:
        if client and adapter.get("client") != client:
            continue
        target = Path(str(adapter["path"])).expanduser()
        if not target.is_absolute():
            target = manifest_path.parent / target
        generated = render_adapter(manifest, adapter, manifest_path=manifest_path)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        merged = merge_generated_block(existing, generated)
        changed = merged != existing
        if changed and BEGIN_MARKER in existing and not check and not force:
            raise ContextAdapterError(
                f"generated context is stale in {target}; inspect with `agent code context check` and rerun install with --force"
            )
        if changed and not check:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(merged, encoding="utf-8")
            tmp.replace(target)
        rows.append(
            {
                "client": adapter.get("client", ""),
                "path": str(target),
                "status": "stale" if changed and check else ("updated" if changed else "current"),
                "changed": changed,
            }
        )
    overlaps = context_overlaps(manifest, manifest_path=manifest_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest": str(manifest_path),
        "canonical_hash": canonical_hash(manifest, manifest_path=manifest_path),
        "precedence": manifest["precedence"],
        "overlaps": overlaps,
        "check": check,
        "ok": bool(rows) and not overlaps and (not check or not any(row["changed"] for row in rows)),
        "adapters": rows,
    }
