#!/usr/bin/env python3
"""Minimal async mailbox for the Claude <-> Codex bridge.

JSONL, stdlib-only. Each line is one message: id, ts, from, to, subject, body.
This is the durable handoff channel — the two CLIs are separate vendor apps with
no live IPC, so collaboration happens by writing/reading messages here and by
one-shot headless invocations (see ask_codex.sh / ask_claude.sh).

Usage:
  mailbox.py send --from claude --to codex --subject "task" --body "..."
  mailbox.py read [--to codex] [--unread]        # print messages
  mailbox.py last [--to claude]                  # print the most recent match
"""
from __future__ import annotations

import argparse
import json
import os

try:
    from .correlation import add_meta_args, extract_meta, format_meta, iso_now, match_meta
    from .trace import emit_event
except ImportError:
    from correlation import add_meta_args, extract_meta, format_meta, iso_now, match_meta  # type: ignore[no-redef]
    from trace import emit_event  # type: ignore[no-redef]

STATE_DIR = os.path.expanduser(os.environ.get("AGENT_BRIDGE_STATE_DIR", "~/.local/state/agent-bridge"))
MAILBOX = os.path.join(STATE_DIR, "mailbox", "messages.jsonl")


def _now() -> str:
    return iso_now()


def _load() -> list[dict]:
    if not os.path.exists(MAILBOX):
        return []
    out = []
    with open(MAILBOX, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _append(msg: dict) -> None:
    os.makedirs(os.path.dirname(MAILBOX), exist_ok=True)
    with open(MAILBOX, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg) + "\n")


def _rewrite(msgs: list[dict]) -> None:
    os.makedirs(os.path.dirname(MAILBOX), exist_ok=True)
    tmp = MAILBOX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for msg in msgs:
            fh.write(json.dumps(msg) + "\n")
    os.replace(tmp, MAILBOX)


def send_message(
    *,
    frm: str,
    to: str,
    subject: str,
    body: str,
    meta: dict | None = None,
    ref: str | None = None,
    status: str | None = None,
) -> dict:
    n = len(_load())
    msg = {"id": f"m{n + 1:04d}", "ts": _now(), "from": frm, "to": to, "subject": subject, "body": body}
    if meta:
        msg["meta"] = meta
    if ref:
        msg["ref"] = ref
    if status:
        msg["status"] = status
    _append(msg)
    emit_event("message.sent", run_id=(meta or {}).get("run_id"), meta=meta, data={"message_id": msg["id"], "from": frm, "to": to, "subject": subject})
    return msg


def filter_messages(
    *,
    to: str | None = None,
    unread_only: bool = False,
    run_id: str | None = None,
    loop_id: str | None = None,
) -> list[dict]:
    filters = {"run_id": run_id, "loop_id": loop_id}
    msgs = _load()
    if to:
        msgs = [m for m in msgs if m["to"] == to]
    if unread_only:
        msgs = [m for m in msgs if m.get("status", "unread") != "read"]
    return [m for m in msgs if match_meta(m, filters)]


def mark_read(message_id: str) -> dict | None:
    msgs = _load()
    found = None
    for msg in msgs:
        if msg["id"] == message_id:
            found = msg
            msg["status"] = "read"
    if found is not None:
        _rewrite(msgs)
    return found


def cmd_send(a) -> int:
    msg = send_message(
        frm=a.frm,
        to=a.to,
        subject=a.subject,
        body=a.body,
        meta=extract_meta(a),
        ref=a.ref,
    )
    print(msg["id"])
    return 0


def _print(m: dict, full: bool) -> None:
    print(f"[{m['id']}] {m['ts']}  {m['from']} -> {m['to']}: {m['subject']}")
    meta = m.get("meta")
    if meta:
        print("  meta: " + format_meta(meta))
    body = m["body"] if full else (m["body"][:500] + ("…" if len(m["body"]) > 500 else ""))
    print("  " + body.replace("\n", "\n  "))


def _filters(a) -> dict:
    return {"run_id": getattr(a, "run_id", None), "loop_id": getattr(a, "loop_id", None)}


def cmd_read(a) -> int:
    msgs = filter_messages(to=a.to, run_id=a.run_id, loop_id=a.loop_id)
    for m in msgs:
        _print(m, a.full)
    if not msgs:
        print("(mailbox empty)")
    return 0


def cmd_last(a) -> int:
    msgs = filter_messages(to=a.to, run_id=a.run_id, loop_id=a.loop_id)
    if not msgs:
        print("(no matching message)")
        return 1
    _print(msgs[-1], True)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Claude<->Codex bridge mailbox")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--subject", default="")
    s.add_argument("--body", required=True)
    s.add_argument("--ref", default=None)
    add_meta_args(s)
    s.set_defaults(func=cmd_send)
    r = sub.add_parser("read")
    r.add_argument("--to", default=None)
    r.add_argument("--full", action="store_true")
    add_meta_args(r, filters_only=True)
    r.set_defaults(func=cmd_read)
    l = sub.add_parser("last")
    l.add_argument("--to", default=None)
    add_meta_args(l, filters_only=True)
    l.set_defaults(func=cmd_last)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
