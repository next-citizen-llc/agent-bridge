from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_bridge import pointer_sync


def _runtime() -> str:
    return "windows-native" if os.name == "nt" else "linux-native"


def _runtime_target(shared: Path, machine: str) -> Path:
    return shared / pointer_sync.ARCHIVE_DIR / pointer_sync.ARCHIVE_VERSION / "machines" / machine / _runtime()


def _write_codex_fixture(
    root: Path,
    *,
    project: Path,
    thread_id: str = "thread-1",
    title: str = "Safe pointer title",
    updated_at: int = 1_800_000_000,
) -> Path:
    codex_home = root / ".codex"
    codex_home.mkdir(parents=True)
    state = {
        "local-projects": {
            "project-1": {
                "name": "demo-project",
                "rootPaths": [str(project)],
            }
        },
        "project-order": ["project-1"],
    }
    (codex_home / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")
    connection = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                title TEXT,
                name TEXT,
                updated_at INTEGER,
                cwd TEXT,
                source TEXT,
                archived INTEGER,
                is_pinned INTEGER,
                git_origin_url TEXT,
                first_user_message TEXT,
                preview TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO threads (
                id, title, name, updated_at, cwd, source, archived, is_pinned,
                git_origin_url, first_user_message, preview
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                title,
                "",
                updated_at,
                str(project),
                "cli",
                0,
                1,
                "https://secret-user:secret-token@github.com/example/demo.git",
                "FIRST_MESSAGE_MUST_NOT_PUBLISH",
                "PREVIEW_MUST_NOT_PUBLISH",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return codex_home


class PointerSyncTests(unittest.TestCase):
    def test_publish_is_pointer_only_and_field_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "demo-project"
            project.mkdir()
            codex_home = _write_codex_fixture(base / "runtime", project=project)
            shared = base / "SharedAgentData"
            shared.mkdir()
            registry = base / "projects.json"
            registry.write_text('{"projects": []}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(base / "state")}, clear=False):
                result = pointer_sync.publish_pointer_snapshot(
                    codex_home=codex_home,
                    shared_root=shared,
                    project_registry=registry,
                    machine_id="test-machine",
                    runtime_id=_runtime(),
                    discover_code_roots=False,
                )
            self.assertEqual(result["status"], "published")
            target = _runtime_target(shared, "test-machine")
            all_text = "\n".join(
                path.read_text(encoding="utf-8") for path in target.rglob("*") if path.is_file()
            )
            self.assertNotIn("FIRST_MESSAGE_MUST_NOT_PUBLISH", all_text)
            self.assertNotIn("PREVIEW_MUST_NOT_PUBLISH", all_text)
            self.assertNotIn("secret-token", all_text)
            self.assertNotIn("codex_home", all_text)
            self.assertNotIn("hostname", all_text)
            source = pointer_sync._load_source(target)
            self.assertIsNotNone(source)
            conversations = source["conversations"]
            projects = source["projects"]
            self.assertEqual(set(conversations[0]), pointer_sync.PUBLISHED_CONVERSATION_FIELDS)
            self.assertEqual(set(projects[0]), pointer_sync.PUBLISHED_PROJECT_FIELDS)

    def test_sources_are_hash_checked_and_recent_rows_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "demo-project"
            project.mkdir()
            shared = base / "SharedAgentData"
            shared.mkdir()
            registry = base / "projects.json"
            registry.write_text('{"projects": []}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(base / "state")}, clear=False):
                for machine, title, updated in (
                    ("machine-a", "older", 1_800_000_000),
                    ("machine-b", "newer", 1_800_000_100),
                ):
                    home = _write_codex_fixture(
                        base / machine,
                        project=project,
                        thread_id="same-thread",
                        title=title,
                        updated_at=updated,
                    )
                    pointer_sync.publish_pointer_snapshot(
                        codex_home=home,
                        shared_root=shared,
                        project_registry=registry,
                        machine_id=machine,
                        runtime_id=_runtime(),
                        discover_code_roots=False,
                    )
            recent = pointer_sync.recent_pointers(shared_root=shared, limit=10)
            self.assertEqual(recent["count"], 1)
            self.assertEqual(recent["conversations"][0]["title"], "newer")
            self.assertEqual(len(recent["conversations"][0]["available_on"]), 2)
            target = _runtime_target(shared, "machine-a")
            current = json.loads((target / "current.json").read_text(encoding="utf-8"))
            corrupt = target / "generations" / current["generation_id"] / "conversations.jsonl"
            corrupt.write_text("{}\n", encoding="utf-8")
            sources = pointer_sync.list_pointer_sources(shared_root=shared)
            self.assertEqual([row["machine_id"] for row in sources], ["machine-b"])

    def test_reader_falls_back_when_current_generation_arrives_before_its_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "demo-project"
            project.mkdir()
            codex_home = _write_codex_fixture(base / "runtime", project=project)
            shared = base / "SharedAgentData"
            shared.mkdir()
            registry = base / "projects.json"
            registry.write_text('{"projects": []}', encoding="utf-8")
            options = {
                "codex_home": codex_home,
                "shared_root": shared,
                "project_registry": registry,
                "machine_id": "test-machine",
                "runtime_id": _runtime(),
                "discover_code_roots": False,
            }
            with mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(base / "state")}, clear=False):
                first = pointer_sync.publish_pointer_snapshot(**options)
                second = pointer_sync.publish_pointer_snapshot(**options)
            target = _runtime_target(shared, "test-machine")
            newest = target / "generations" / second["generation_id"] / "projects.jsonl"
            newest.write_text("not-synced-yet\n", encoding="utf-8")
            source = pointer_sync._load_source(target)
            self.assertIsNotNone(source)
            self.assertEqual(source["manifest"]["generation_id"], first["generation_id"])

    def test_lightweight_publish_preserves_last_full_project_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "demo-project"
            project.mkdir()
            codex_home = _write_codex_fixture(base / "runtime", project=project)
            shared = base / "SharedAgentData"
            shared.mkdir()
            registry = base / "projects.json"
            registry.write_text('{"projects": []}', encoding="utf-8")
            options = {
                "codex_home": codex_home,
                "shared_root": shared,
                "project_registry": registry,
                "machine_id": "test-machine",
                "runtime_id": _runtime(),
            }
            with mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(base / "state")}, clear=False):
                full = pointer_sync.publish_pointer_snapshot(**options, discover_code_roots=False)
                (codex_home / ".codex-global-state.json").write_text("{}", encoding="utf-8")
                light = pointer_sync.publish_pointer_snapshot(
                    **options,
                    discover_code_roots=False,
                    discover_thread_roots=False,
                    preserve_existing_projects=True,
                )
                authoritative = pointer_sync.publish_pointer_snapshot(
                    **options,
                    discover_code_roots=False,
                    discover_thread_roots=False,
                )
            self.assertEqual(full["project_count"], 1)
            self.assertEqual(light["project_count"], 1)
            self.assertEqual(authoritative["project_count"], 0)

    def test_path_and_remote_normalization_enforce_native_portable_aliases(self) -> None:
        self.assertFalse(pointer_sync._path_compatible("/Users/tts/Code/demo", "windows-native"))
        self.assertFalse(pointer_sync._path_compatible(r"\\wsl$\Ubuntu\home\thist\code\demo", "windows-native"))
        self.assertTrue(pointer_sync._path_compatible(r"C:\Users\thist\Code\demo", "windows-native"))
        self.assertEqual(
            pointer_sync._normalize_git_remote("https://token@github.com/Example/Demo.git?secret=yes"),
            "github.com/example/demo",
        )
        self.assertEqual(pointer_sync._normalize_git_remote("git@github.com:Example/Demo.git"), "github.com/example/demo")
        self.assertEqual(pointer_sync._normalize_git_remote(r"C:\private\demo.git"), "")
        self.assertEqual(pointer_sync._normalize_git_remote("/private/demo.git"), "")
        self.assertEqual(pointer_sync._normalize_git_remote("../private/demo.git"), "")
        self.assertEqual(pointer_sync._normalize_git_remote("~/private/demo.git"), "")

    def test_scheduler_command_pins_agent_shared_root_and_codex_home(self) -> None:
        with mock.patch.object(pointer_sync, "_agent_command", return_value="/home/test/.local/bin/agent"):
            command = pointer_sync._scheduler_command(
                shared_root=Path("/mnt/c/shared"),
                codex_home=Path("/home/test/.codex"),
                recent_limit=75,
            )
        self.assertEqual(command[0], "/home/test/.local/bin/agent")
        self.assertIn(str(Path("/mnt/c/shared")), command)
        self.assertIn(str(Path("/home/test/.codex")), command)
        self.assertIn("--no-code-scan", command)
        self.assertEqual(command[-1], "--quiet")

    def test_windows_batch_arguments_quote_spaces_and_escape_percent(self) -> None:
        self.assertEqual(pointer_sync._windows_batch_arg(r"C:\My %USER%\agent.cmd"), r'"C:\My %%USER%%\agent.cmd"')

    def test_systemd_exec_path_is_quoted_and_percent_escaped(self) -> None:
        self.assertEqual(
            pointer_sync._systemd_exec_arg('/home/test user/.local/%agent'),
            '"/home/test user/.local/%%agent"',
        )

    def test_startup_candidate_scan_can_exclude_historical_thread_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "historical-project"
            project.mkdir()
            threads = [{"cwd": str(project), "updated_at": 1_800_000_000}]
            excluded = pointer_sync._candidate_projects(
                root,
                {},
                threads,
                runtime=_runtime(),
                discover_code_roots=False,
                discover_thread_roots=False,
            )
            included = pointer_sync._candidate_projects(
                root,
                {},
                threads,
                runtime=_runtime(),
                discover_code_roots=False,
                discover_thread_roots=True,
            )
        self.assertEqual(excluded, [])
        self.assertEqual(included[0][2], "recent-thread")


if __name__ == "__main__":
    unittest.main()
