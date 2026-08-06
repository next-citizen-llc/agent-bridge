from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge.coord import (
    capability_card,
    daemon_status,
    evaluate_policy,
    event_envelope,
    explain_incompatibility,
    make_delivery,
    run_coordination_eval,
    sign_payload,
    task_attach_artifact,
    task_cancel,
    task_claim,
    task_create,
    task_inspect,
    task_request_input,
    task_resume,
    transport_ack,
    transport_receive,
    transport_send,
    transport_smoke,
    verify_payload,
)
from agent_bridge.trace import emit_event


class CoordinationPrimitiveTests(unittest.TestCase):
    def test_capability_card_reports_incompatible_modes_and_media(self) -> None:
        card = capability_card({"id": "reader", "adapter": "argv", "command": "python3", "modes": ["review"]})
        problems = explain_incompatibility(card, mode="code", media_suffixes=[".png"])

        self.assertEqual(card["id"], "reader")
        self.assertIn("review", card["modes"])
        self.assertTrue(any("mode 'code'" in problem for problem in problems))
        self.assertTrue(any("media .png" in problem for problem in problems))

    def test_task_ledger_lifecycle_keeps_artifacts_and_input_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": tmp}):
            event = task_create("fix bridge", run_id="run-a", owner="builder")
            task_id = event["task_id"]
            task_claim(task_id, owner="critic")
            task_request_input(task_id, question="Need approval?")
            task_attach_artifact(task_id, path="artifacts/report.md", kind="report")
            needs_input = task_inspect(task_id)
            task_resume(task_id, note="approved")
            resumed = task_inspect(task_id)
            task_cancel(task_id, reason="superseded")
            cancelled = task_inspect(task_id)

        self.assertEqual(needs_input["status"], "needs_input")
        self.assertEqual(needs_input["artifacts"][0]["kind"], "report")
        self.assertEqual(resumed["pending_input"], [])
        self.assertEqual(cancelled["status"], "cancelled")

    def test_trace_envelope_preserves_run_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": tmp}):
            event = emit_event("agent.dispatched", run_id="run-a", meta={"run_id": "run-a", "turn_id": "turn-a"})
            envelope = event_envelope(event)

        self.assertEqual(envelope["subject"], "run-a")
        self.assertEqual(envelope["meta"]["turn_id"], "turn-a")
        self.assertRegex(envelope["traceparent"], r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")

    def test_policy_deny_and_hmac_verification(self) -> None:
        decision = evaluate_policy(
            {"client": "remote", "action": "dispatch"},
            policies={"default": "allow", "rules": [{"client": "remote", "decision": "deny", "reason": "remote blocked"}]},
            trace=False,
        )
        payload = {"task": "inspect"}
        signature = sign_payload(payload, key=b"secret")

        self.assertEqual(decision["decision"], "deny")
        self.assertTrue(verify_payload(payload, signature, key=b"secret")["verified"])
        self.assertFalse(verify_payload({"task": "mutated"}, signature, key=b"secret")["verified"])

    def test_transport_dedup_ack_and_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = make_delivery({"hello": "world"}, source="test", message_type="test.message", dedupe_key="same")
            first = transport_send("queue", envelope, root=tmp)
            duplicate = transport_send("queue", dict(envelope, id="different"), root=tmp)
            pending = transport_receive("queue", root=tmp)
            transport_ack("queue", envelope["id"], root=tmp)
            after_ack = transport_receive("queue", root=tmp)
            messages = Path(tmp) / "queue" / "messages.jsonl"
            messages.write_text(messages.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8")
            corrupt = transport_receive("queue", root=tmp)

        self.assertEqual(first["status"], "sent")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(len(pending["pending"]), 1)
        self.assertEqual(after_ack["pending"], [])
        self.assertEqual(corrupt["corrupt_lines"], 1)

    def test_eval_harness_runs_eight_scenarios_and_daemon_stays_optional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": tmp}):
            scorecard = run_coordination_eval()
            smoke = transport_smoke(root=str(Path(tmp) / "transport"))
            daemon = daemon_status()

        self.assertEqual(scorecard["total"], 8)
        self.assertEqual(scorecard["failed"], 0)
        self.assertTrue(smoke["ok"])
        self.assertTrue(daemon["daemon_optional"])


if __name__ == "__main__":
    unittest.main()
