from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAILBOX = ROOT / "agent_bridge" / "mailbox.py"


class MailboxTests(unittest.TestCase):
    def test_send_and_read_use_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": tmp}
            send = subprocess.run(
                [
                    "python3",
                    str(MAILBOX),
                    "send",
                    "--from",
                    "codex",
                    "--to",
                    "claude",
                    "--subject",
                    "smoke",
                    "--body",
                    "hello",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(send.returncode, 0, send.stderr)
            self.assertEqual(send.stdout.strip(), "m0001")
            self.assertTrue((Path(tmp) / "mailbox" / "messages.jsonl").exists())

            read = subprocess.run(
                ["python3", str(MAILBOX), "read", "--to", "claude", "--full"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertIn("codex -> claude: smoke", read.stdout)
            self.assertIn("hello", read.stdout)

    def test_send_read_filter_correlation_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": tmp}
            send = subprocess.run(
                [
                    "python3",
                    str(MAILBOX),
                    "send",
                    "--from",
                    "codex",
                    "--to",
                    "claude",
                    "--subject",
                    "correlated",
                    "--body",
                    "hello",
                    "--run-id",
                    "run-test",
                    "--loop-id",
                    "loop-test",
                    "--turn-id",
                    "turn-test",
                    "--attempt",
                    "2",
                    "--role",
                    "critic",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(send.returncode, 0, send.stderr)

            read = subprocess.run(
                ["python3", str(MAILBOX), "read", "--to", "claude", "--run-id", "run-test", "--full"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertIn("meta: run_id=run-test loop_id=loop-test turn_id=turn-test attempt=2 role=critic", read.stdout)
            self.assertTrue((Path(tmp) / "events.jsonl").exists())

            miss = subprocess.run(
                ["python3", str(MAILBOX), "read", "--to", "claude", "--run-id", "other"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(miss.returncode, 0, miss.stderr)
            self.assertIn("(mailbox empty)", miss.stdout)

    def test_legacy_messages_without_metadata_still_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mailbox_dir = Path(tmp) / "mailbox"
            mailbox_dir.mkdir()
            (mailbox_dir / "messages.jsonl").write_text(
                '{"id": "m0001", "ts": "2026-01-01T00:00:00Z", "from": "codex", "to": "claude", "subject": "legacy", "body": "old"}\n',
                encoding="utf-8",
            )
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": tmp}
            read = subprocess.run(
                ["python3", str(MAILBOX), "read", "--to", "claude", "--full"],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertIn("legacy", read.stdout)


if __name__ == "__main__":
    unittest.main()
