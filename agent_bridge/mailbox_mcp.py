#!/usr/bin/env python3
"""Dependency-free stdio MCP server exposing the agent mailbox as native tools.

This is the *primary* Claude Code <-> Codex bridge: both CLIs register this
server, so each agent drives the same file-backed mailbox through real MCP
tools (no shell-script middleman). It is still an async shared mailbox, not
live session-injection IPC — the two CLIs are separate vendor apps with no
direct IPC. The same store (`agent_mailbox/messages.jsonl`) is shared with the
`mailbox.py` CLI, so the shell tools and the MCP interoperate.

Implements just enough of MCP (JSON-RPC 2.0 over newline-delimited stdio):
  initialize, notifications/initialized, tools/list, tools/call.
Tools: mailbox_send, mailbox_inbox, mailbox_read.

Register with Codex:
  codex mcp add mailbox -- python3 /Users/tts/Code/agent-bridge/agent_bridge/mailbox_mcp.py
Register with Claude Code globally:
  claude mcp add --scope user mailbox -- python3 /Users/tts/Code/agent-bridge/agent_bridge/mailbox_mcp.py
(Desktop apps need a restart / their own MCP settings to load it.)

Global local dev tooling. Mailbox state is stored under ~/.local/state/agent-bridge/.
"""
import json
import os
import sys

try:
    from . import mailbox as mb
    from .correlation import extract_meta
    from .findings import create_finding, format_findings, format_verdicts, list_findings, list_verdicts, read_finding, record_verdict
    from .trace import format_events, load_events
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mailbox as mb  # type: ignore[no-redef]
    from correlation import extract_meta  # type: ignore[no-redef]
    from findings import create_finding, format_findings, format_verdicts, list_findings, list_verdicts, read_finding, record_verdict  # type: ignore[no-redef]
    from trace import format_events, load_events  # type: ignore[no-redef]

PROTO = "2024-11-05"

TOOLS = [
    {
        "name": "mailbox_send",
        "description": "Send a message to another agent's mailbox (e.g. to=codex or to=claude).",
        "inputSchema": {
            "type": "object",
            "required": ["from", "to", "subject", "body"],
            "properties": {
                "from": {"type": "string"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "ref": {"type": "string", "description": "Optional id of the message this replies to."},
                "run_id": {"type": "string", "description": "Optional correlation: run identifier."},
                "loop_id": {"type": "string", "description": "Optional correlation: loop identifier."},
                "turn_id": {"type": "string", "description": "Optional correlation: turn identifier."},
                "parent_id": {"type": "string", "description": "Optional correlation: parent id."},
                "attempt": {"type": "integer", "description": "Optional correlation: attempt number."},
                "role": {"type": "string", "description": "Optional correlation: sender role."},
            },
        },
    },
    {
        "name": "mailbox_inbox",
        "description": "List messages addressed to an agent (newest last).",
        "inputSchema": {
            "type": "object",
            "required": ["to"],
            "properties": {
                "to": {"type": "string"},
                "unread_only": {"type": "boolean"},
                "run_id": {"type": "string", "description": "Optional filter: only this run_id."},
                "loop_id": {"type": "string", "description": "Optional filter: only this loop_id."},
            },
        },
    },
    {
        "name": "mailbox_read",
        "description": "Read a message by id and mark it read.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
    },
]

TOOLS.extend([
    {
        "name": "trace_events",
        "description": "List structured trace events, optionally filtered by run_id or type.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "type": {"type": "string"},
            },
        },
    },
    {
        "name": "finding_emit",
        "description": "Emit a structured adversarial finding.",
        "inputSchema": {
            "type": "object",
            "required": ["run_id", "severity", "claim"],
            "properties": {
                "run_id": {"type": "string"},
                "severity": {"type": "string"},
                "claim": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "reproduction": {"type": "string"},
                "status": {"type": "string"},
                "owner_role": {"type": "string"},
                "rebuttal": {"type": "string"},
                "resolution": {"type": "string"},
            },
        },
    },
    {
        "name": "findings_list",
        "description": "List structured findings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "status": {"type": "string"},
                "severity": {"type": "string"},
            },
        },
    },
    {
        "name": "finding_read",
        "description": "Read a structured finding by id.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
    },
    {
        "name": "verdict_record",
        "description": "Record an adversarial loop verdict.",
        "inputSchema": {
            "type": "object",
            "required": ["run_id", "status", "summary"],
            "properties": {
                "run_id": {"type": "string"},
                "status": {"type": "string"},
                "summary": {"type": "string"},
                "blocking_findings": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "verdicts_list",
        "description": "List recorded verdicts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "status": {"type": "string"},
            },
        },
    },
])


def _send(args):
    msg = mb.send_message(
        frm=args["from"],
        to=args["to"],
        subject=args["subject"],
        body=args["body"],
        ref=args.get("ref"),
        meta=extract_meta(args),
        status="unread",
    )
    return f'sent {msg["id"]}'


def _inbox(args):
    rows = []
    msgs = mb.filter_messages(
        to=args["to"],
        unread_only=args.get("unread_only", False),
        run_id=args.get("run_id"),
        loop_id=args.get("loop_id"),
    )
    for m in msgs:
        meta = m.get("meta") or {}
        suffix = f' run={meta["run_id"]}' if meta.get("run_id") else ""
        rows.append(f'{m["id"]} [{m.get("status", "unread")}] {m["ts"]} from {m["from"]}: {m["subject"]}{suffix}')
    return "\n".join(rows) if rows else "(empty)"


def _read(args):
    found = mb.mark_read(args["id"])
    if found is not None:
        return json.dumps(found, indent=2)
    return f'no message {args["id"]}'


def _trace_events(args):
    return format_events(load_events(run_id=args.get("run_id"), event_type=args.get("type")))


def _finding_emit(args):
    row = create_finding(
        run_id=args["run_id"],
        severity=args["severity"],
        claim=args["claim"],
        evidence=args.get("evidence"),
        reproduction=args.get("reproduction", ""),
        status=args.get("status", "open"),
        owner_role=args.get("owner_role", ""),
        rebuttal=args.get("rebuttal", ""),
        resolution=args.get("resolution", ""),
    )
    return json.dumps(row, indent=2)


def _findings_list(args):
    return format_findings(list_findings(run_id=args.get("run_id"), status=args.get("status"), severity=args.get("severity")))


def _finding_read(args):
    row = read_finding(args["id"])
    return json.dumps(row, indent=2) if row else f'no finding {args["id"]}'


def _verdict_record(args):
    row = record_verdict(
        run_id=args["run_id"],
        status=args["status"],
        summary=args["summary"],
        blocking_findings=args.get("blocking_findings"),
        evidence=args.get("evidence"),
    )
    return json.dumps(row, indent=2)


def _verdicts_list(args):
    return format_verdicts(list_verdicts(run_id=args.get("run_id"), status=args.get("status")))


DISPATCH = {
    "mailbox_send": _send,
    "mailbox_inbox": _inbox,
    "mailbox_read": _read,
    "trace_events": _trace_events,
    "finding_emit": _finding_emit,
    "findings_list": _findings_list,
    "finding_read": _finding_read,
    "verdict_record": _verdict_record,
    "verdicts_list": _verdicts_list,
}


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        id_ = req.get("id")
        if method == "initialize":
            reply(id_, {
                "protocolVersion": PROTO,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-mailbox", "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(id_, {"tools": TOOLS})
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            fn = DISPATCH.get(name)
            if not fn:
                reply(id_, error={"code": -32601, "message": f"unknown tool {name}"})
                continue
            try:
                text = fn(args)
                reply(id_, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:  # noqa: BLE001
                reply(id_, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif id_ is not None:
            reply(id_, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    main()
