from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_bridge.session_recovery import (
    SessionRecoveryError,
    collect_git_evidence,
    discover_claude_sessions,
    filter_sessions,
    load_recovery_selection,
    recover_sessions,
    redact_text,
)


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "bin" / "agent"


class SessionRecoveryTests(unittest.TestCase):
    def _write_claude_code_session(
        self,
        root: Path,
        *,
        session_id: str,
        native_id: str,
        title: str,
        cwd: Path,
        rows: list[dict[str, object]],
        archived: bool = False,
    ) -> tuple[Path, Path]:
        metadata_dir = root / "Claude" / "claude-code-sessions" / "account" / "workspace"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        metadata = metadata_dir / f"{session_id}.json"
        metadata.write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "cliSessionId": native_id,
                    "cwd": str(cwd),
                    "originCwd": str(cwd),
                    "lastActivityAt": time.time(),
                    "createdAt": time.time() - 60,
                    "isArchived": archived,
                    "title": title,
                    "systemPrompt": "API_KEY=must-not-leak",
                    "emailAddress": "private@example.test",
                }
            ),
            encoding="utf-8",
        )
        transcript_dir = root / "projects" / "encoded-project"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript = transcript_dir / f"{native_id}.jsonl"
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return metadata, transcript

    def _write_desktop_session(self, root: Path, *, cwd: Path) -> None:
        session_id = "local_desktop"
        metadata_dir = root / "Claude" / "local-agent-mode-sessions" / "account" / "workspace"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / f"{session_id}.json").write_text(
            json.dumps(
                {
                    "sessionId": session_id,
                    "cliSessionId": "desktop-native",
                    "cwd": "/missing/vm/path",
                    "userSelectedFolders": [str(cwd)],
                    "lastActivityAt": time.time(),
                    "title": "Desktop application task",
                    "isArchived": False,
                }
            ),
            encoding="utf-8",
        )
        audit_dir = metadata_dir / session_id
        audit_dir.mkdir()
        (audit_dir / "audit.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2030-01-01T00:00:00Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "Finish the application."}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _git_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
        return repo

    def test_inventory_discovers_code_and_desktop_sessions_with_safe_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._git_repo(root)
            self._write_claude_code_session(
                root,
                session_id="local_blocked",
                native_id="11111111-1111-4111-8111-111111111111",
                title="Blocked coding task",
                cwd=repo,
                rows=[
                    {
                        "type": "user",
                        "timestamp": "2030-01-01T00:00:00Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": "Implement the change."}]},
                    },
                    {
                        "type": "assistant",
                        "timestamp": "2030-01-01T00:00:01Z",
                        "isApiErrorMessage": True,
                        "apiErrorStatus": "You've hit your usage limit",
                        "message": {"role": "assistant", "content": []},
                    },
                ],
            )
            self._write_desktop_session(root, cwd=repo)
            sessions = discover_claude_sessions(
                data_root=root / "Claude",
                projects_root=root / "projects",
            )

        indexed = {row["session_id"]: row for row in sessions}
        self.assertEqual(indexed["local_blocked"]["signal"]["status"], "blocked_usage_limit")
        self.assertEqual(indexed["local_desktop"]["signal"]["status"], "awaiting_assistant")
        self.assertEqual(indexed["local_desktop"]["transcript_kind"], "claude_desktop_audit")
        self.assertEqual(indexed["local_desktop"]["project_candidates"][0], str(repo))
        self.assertNotIn("systemPrompt", indexed["local_blocked"])
        self.assertNotIn("emailAddress", indexed["local_blocked"])

    def test_filter_explicit_session_bypasses_age_and_archive_filters(self) -> None:
        rows = [
            {
                "session_id": "old",
                "title": "Old task",
                "archived": True,
                "last_activity_epoch": 1.0,
            }
        ]
        self.assertEqual(filter_sessions(rows, now=10_000.0), [])
        selected = filter_sessions(rows, session_ids=["old"], now=10_000.0)
        self.assertEqual([row["session_id"] for row in selected], ["old"])

    def test_redaction_covers_fields_bearer_urls_tokens_and_private_keys(self) -> None:
        fake_github_token = "ghp_" + ("1" * 30)
        private_key_block = (
            "-----BEGIN " + "PRIVATE KEY-----\nprivate\n-----END " + "PRIVATE KEY-----"
        )
        raw = (
            "API_KEY=supersecret Authorization: BasicSecret Bearer abc.def.ghi "
            f"https://example.test/?token=visible {fake_github_token} {private_key_block}"
        )
        redacted = redact_text(raw)
        for secret in ("supersecret", "BasicSecret", "abc.def.ghi", "visible", "ghp_123", "private\n"):
            self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_recovery_stages_only_continuations_and_enqueues_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._git_repo(root)
            _, continue_transcript = self._write_claude_code_session(
                root,
                session_id="local_continue",
                native_id="22222222-2222-4222-8222-222222222222",
                title="Continue task",
                cwd=repo,
                rows=[
                    {
                        "type": "user",
                        "timestamp": "2030-01-01T00:00:00Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "Finish this. api_key=do-not-copy"}],
                        },
                    }
                ],
            )
            self._write_claude_code_session(
                root,
                session_id="local_complete",
                native_id="33333333-3333-4333-8333-333333333333",
                title="Completed task",
                cwd=repo,
                rows=[
                    {
                        "type": "assistant",
                        "timestamp": "2030-01-01T00:00:00Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
                    }
                ],
            )
            sessions = discover_claude_sessions(data_root=root / "Claude", projects_root=root / "projects")
            state = root / "state"
            output = root / "recoveries"
            with patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(state)}, clear=False):
                first = recover_sessions(
                    sessions,
                    decisions={"local_continue": "continue", "local_complete": "complete"},
                    output_root=output,
                    enqueue=True,
                )
                second = recover_sessions(
                    sessions,
                    decisions={"local_continue": "continue"},
                    output_root=output,
                    enqueue=True,
                )
                with continue_transcript.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "type": "user",
                                "timestamp": "2030-01-02T00:00:00Z",
                                "message": {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "A genuinely new turn."}],
                                },
                            }
                        )
                        + "\n"
                    )
                third = recover_sessions(
                    sessions,
                    decisions={"local_continue": "continue"},
                    output_root=output,
                    enqueue=True,
                )
            continue_row = next(row for row in first["sessions"] if row["session_id"] == "local_continue")
            complete_row = next(row for row in first["sessions"] if row["session_id"] == "local_complete")
            handoff = Path(continue_row["handoff_path"]).read_text(encoding="utf-8")
            task_rows = [json.loads(line) for line in (state / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertFalse(first["native_history_imported"])
        self.assertTrue(continue_row["handoff_path"])
        self.assertEqual(continue_row["queue"]["status"], "created")
        self.assertFalse(complete_row["handoff_path"])
        self.assertEqual(complete_row["queue"]["status"], "not_requested")
        self.assertIn("Finish this", handoff)
        self.assertNotIn("do-not-copy", handoff)
        self.assertEqual(second["sessions"][0]["queue"]["status"], "existing")
        self.assertEqual(third["sessions"][0]["queue"]["status"], "created")
        self.assertEqual(sum(1 for row in task_rows if row["event"] == "created"), 2)

    def test_git_evidence_records_dirty_paths_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._git_repo(Path(tmp))
            (repo / "pending.txt").write_text("pending\n", encoding="utf-8")
            evidence = collect_git_evidence(repo)
        self.assertEqual(evidence["status"], "ok")
        self.assertFalse(evidence["worktree_clean"])
        self.assertIn("?? pending.txt", evidence["worktree_status"])
        self.assertEqual(evidence["github"]["status"], "not_checked")

    def test_recovery_refuses_to_write_private_handoffs_inside_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._git_repo(root)
            self._write_claude_code_session(
                root,
                session_id="local_private",
                native_id="55555555-5555-4555-8555-555555555555",
                title="Private handoff",
                cwd=repo,
                rows=[],
            )
            sessions = discover_claude_sessions(data_root=root / "Claude", projects_root=root / "projects")
            with self.assertRaisesRegex(SessionRecoveryError, "outside Git worktrees"):
                recover_sessions(
                    sessions,
                    decisions={"local_private": "continue"},
                    output_root=repo / "private-recovery",
                )

    def test_selection_file_and_cli_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._git_repo(root)
            self._write_claude_code_session(
                root,
                session_id="local_cli",
                native_id="44444444-4444-4444-8444-444444444444",
                title="CLI fixture",
                cwd=repo,
                rows=[],
            )
            selection = root / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "sessions": [
                            {"session_id": "local_cli", "disposition": "continue", "project_dir": str(repo)}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            decisions, projects = load_recovery_selection(selection)
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "sessions",
                    "inventory",
                    "--claude-data-root",
                    str(root / "Claude"),
                    "--claude-projects-root",
                    str(root / "projects"),
                    "--session-id",
                    "local_cli",
                    "--json",
                ],
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            recover_proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "sessions",
                    "recover",
                    "--selection",
                    str(selection),
                    "--claude-data-root",
                    str(root / "Claude"),
                    "--claude-projects-root",
                    str(root / "projects"),
                    "--output-root",
                    str(root / "recovery-output"),
                    "--json",
                ],
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            recovery = json.loads(recover_proc.stdout) if recover_proc.returncode == 0 else {}
            recovery_handoff_exists = bool(recovery) and Path(recovery["sessions"][0]["handoff_path"]).is_file()
        self.assertEqual(decisions, {"local_cli": "continue"})
        self.assertEqual(projects, {"local_cli": str(repo)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload[0]["session_id"], "local_cli")
        self.assertEqual(recover_proc.returncode, 0, recover_proc.stderr)
        self.assertFalse(recovery["native_history_imported"])
        self.assertTrue(recovery_handoff_exists)


if __name__ == "__main__":
    unittest.main()
