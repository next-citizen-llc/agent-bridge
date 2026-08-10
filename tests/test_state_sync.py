from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_bridge.state_sync import (
    CHUNK_SIZE,
    SCHEDULER_LABEL,
    StateSyncError,
    _atomic_write_bytes,
    _apply_lock,
    _codex_desktop_running,
    _install_artifact,
    _macos_plist,
    _merge_session_index,
    _pid_is_running,
    _powershell_literal,
    _publisher_lock,
    _read_native_json_object,
    _scheduler_arguments,
    _windows_scheduler_script,
    apply_codex_state,
    list_state_sync_sources,
    publish_codex_state,
)


SOURCE_THREAD = "11111111-1111-4111-8111-111111111111"
TARGET_THREAD = "22222222-2222-4222-8222-222222222222"


class StateSyncTests(unittest.TestCase):
    def _create_db(self, home: Path, rows: list[dict[str, object]]) -> Path:
        home.mkdir(parents=True, exist_ok=True)
        db = home / "state_5.sqlite"
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    model_provider TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    sandbox_policy TEXT NOT NULL,
                    approval_mode TEXT NOT NULL,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0,
                    preview TEXT NOT NULL DEFAULT '',
                    is_pinned INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER
                )
                """
            )
            for row in rows:
                names = list(row)
                connection.execute(
                    f"INSERT INTO threads ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                    [row[name] for name in names],
                )
            connection.commit()
        finally:
            connection.close()
        return db

    def _thread_row(
        self,
        thread_id: str,
        rollout_path: Path,
        cwd: str,
        *,
        title: str,
        updated_at: int,
    ) -> dict[str, object]:
        return {
            "id": thread_id,
            "rollout_path": str(rollout_path),
            "created_at": updated_at - 10,
            "updated_at": updated_at,
            "source": "vscode",
            "model_provider": "openai",
            "cwd": cwd,
            "title": title,
            "sandbox_policy": "danger-full-access",
            "approval_mode": "never",
            "tokens_used": 10,
            "archived": 0,
            "preview": title,
            "is_pinned": 0,
            "created_at_ms": (updated_at - 10) * 1000,
            "updated_at_ms": updated_at * 1000,
        }

    def _source_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        source_home = root / "source-codex"
        source_project = root / "source-project"
        source_project.mkdir()
        target_project = root / "target-project"
        target_project.mkdir()
        session = source_home / "sessions" / "2030" / "01" / "01" / f"rollout-{SOURCE_THREAD}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_bytes((b"x" * CHUNK_SIZE) + b'{"type":"user","text":"first"}\n')
        self._create_db(
            source_home,
            [
                self._thread_row(
                    SOURCE_THREAD,
                    session,
                    str(source_project / "nested"),
                    title="Source session",
                    updated_at=2_000,
                )
            ],
        )
        (source_home / ".codex-global-state.json").write_text(
            json.dumps(
                {
                    "project-order": ["source-project-id"],
                    "local-projects": {
                        "source-project-id": {
                            "id": "source-project-id",
                            "name": "Shared Project",
                            "rootPaths": [str(source_project)],
                            "createdAt": 1,
                            "updatedAt": 2,
                        }
                    },
                    "thread-project-assignments": {
                        SOURCE_THREAD: {
                            "projectKind": "local",
                            "projectId": "source-project-id",
                            "path": str(source_project),
                            "cwd": str(source_project),
                            "pendingCoreUpdate": False,
                        }
                    },
                    "thread-workspace-root-hints": {SOURCE_THREAD: str(source_project)},
                    "sidebar-project-thread-orders": {"source-project-id": [SOURCE_THREAD]},
                    "pinned-thread-ids": [SOURCE_THREAD],
                    "projectless-thread-ids": [],
                }
            ),
            encoding="utf-8",
        )
        registry = root / "projects.json"
        registry.write_text(
            json.dumps(
                {
                    "version": 2,
                    "projects": [
                        {
                            "slug": "shared-project",
                            "name": "Shared Project",
                            "workspace_macos": str(source_project),
                            "workspace_windows": str(target_project),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return source_home, source_project, session, registry

    def _target_fixture(self, root: Path) -> tuple[Path, Path]:
        target_home = root / "target-codex"
        target_session = target_home / "sessions" / "target.jsonl"
        target_session.parent.mkdir(parents=True)
        target_session.write_text("target-only\n", encoding="utf-8")
        self._create_db(
            target_home,
            [
                self._thread_row(
                    TARGET_THREAD,
                    target_session,
                    str(root / "target-only-project"),
                    title="Target only",
                    updated_at=3_000,
                )
            ],
        )
        (target_home / ".codex-global-state.json").write_text(
            json.dumps(
                {
                    "project-order": ["target-only-project-id"],
                    "local-projects": {
                        "target-only-project-id": {
                            "id": "target-only-project-id",
                            "name": "Target Only",
                            "rootPaths": [str(root / "target-only-project")],
                            "createdAt": 1,
                            "updatedAt": 2,
                        }
                    },
                    "thread-project-assignments": {},
                    "thread-workspace-root-hints": {},
                    "sidebar-project-thread-orders": {},
                    "pinned-thread-ids": [],
                    "projectless-thread-ids": [TARGET_THREAD],
                }
            ),
            encoding="utf-8",
        )
        return target_home, target_session

    def test_publish_reuses_baseline_and_transmits_only_changed_tail_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            source_home, _, session, registry = self._source_fixture(root)

            first = publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            object_root = Path(first["archive_root"]) / "objects" / "sha256"
            initial_objects = {path for path in object_root.rglob("*.gz")}
            self.assertEqual(first["new_objects"], 2)
            self.assertEqual(first["referenced_object_count"], 2)

            second = publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            self.assertEqual(second["new_objects"], 0)
            self.assertEqual(second["new_stored_bytes"], 0)

            with session.open("ab") as handle:
                handle.write(b'{"type":"assistant","text":"delta"}\n')
            connection = sqlite3.connect(source_home / "state_5.sqlite")
            try:
                connection.execute(
                    "UPDATE threads SET updated_at = ?, updated_at_ms = ? WHERE id = ?",
                    (2_001, 2_001_000, SOURCE_THREAD),
                )
                connection.commit()
            finally:
                connection.close()

            third = publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            self.assertEqual(third["new_objects"], 1)
            self.assertEqual(third["reused_objects"], 1)
            self.assertTrue(initial_objects.issubset({path for path in object_root.rglob("*.gz")}))
            self.assertEqual(len({path for path in object_root.rglob("*.gz")}), 3)

    def test_apply_is_additive_idempotent_and_remaps_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            source_home, _, source_session, registry = self._source_fixture(root)
            target_home, target_session = self._target_fixture(root)
            publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )

            with patch("agent_bridge.state_sync._codex_desktop_running", return_value=False):
                first = apply_codex_state(
                    codex_home=target_home,
                    shared_root=shared,
                    project_registry=registry,
                    source_machines=["source-machine"],
                    current_machine="target-machine",
                    platform_name="windows",
                    yes=True,
                )
                second = apply_codex_state(
                    codex_home=target_home,
                    shared_root=shared,
                    project_registry=registry,
                    source_machines=["source-machine"],
                    current_machine="target-machine",
                    platform_name="windows",
                    yes=True,
                )

            self.assertEqual(first["thread_results"]["inserted"], 1)
            self.assertEqual(second["thread_results"]["inserted"], 0)
            self.assertEqual(second["artifact_results"]["unchanged"], 1)
            self.assertEqual(target_session.read_text(encoding="utf-8"), "target-only\n")

            connection = sqlite3.connect(target_home / "state_5.sqlite")
            connection.row_factory = sqlite3.Row
            try:
                rows = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM threads")}
            finally:
                connection.close()
            self.assertEqual(set(rows), {SOURCE_THREAD, TARGET_THREAD})
            self.assertEqual(rows[SOURCE_THREAD]["cwd"], str(root / "target-project" / "nested"))
            imported_session = Path(rows[SOURCE_THREAD]["rollout_path"])
            self.assertEqual(imported_session.read_bytes(), source_session.read_bytes())

            state = json.loads((target_home / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertIn("target-only-project-id", state["local-projects"])
            assignment = state["thread-project-assignments"][SOURCE_THREAD]
            self.assertEqual(assignment["path"], str(root / "target-project"))
            self.assertIn(SOURCE_THREAD, state["pinned-thread-ids"])
            self.assertTrue(Path(first["backup"]).is_dir())

            connection = sqlite3.connect(target_home / "state_5.sqlite")
            try:
                connection.execute(
                    "UPDATE threads SET updated_at = ?, updated_at_ms = ? WHERE id = ?",
                    (5_000, 5_000_000, SOURCE_THREAD),
                )
                connection.commit()
            finally:
                connection.close()
            state["thread-project-assignments"][SOURCE_THREAD] = {
                "projectKind": "local",
                "projectId": "target-only-project-id",
                "path": str(root / "target-only-project"),
                "cwd": str(root / "target-only-project"),
                "pendingCoreUpdate": False,
            }
            (target_home / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")
            with patch("agent_bridge.state_sync._codex_desktop_running", return_value=False):
                local_newer = apply_codex_state(
                    codex_home=target_home,
                    shared_root=shared,
                    project_registry=registry,
                    source_machines=["source-machine"],
                    current_machine="target-machine",
                    platform_name="windows",
                    yes=True,
                )
            self.assertEqual(local_newer["thread_results"]["preserved"], 1)
            final_state = json.loads((target_home / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                final_state["thread-project-assignments"][SOURCE_THREAD]["projectId"],
                "target-only-project-id",
            )

    def test_divergent_session_is_preserved_and_staged_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            source_home, _, source_session, registry = self._source_fixture(root)
            target_home, _ = self._target_fixture(root)
            publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            with patch("agent_bridge.state_sync._codex_desktop_running", return_value=False):
                apply_codex_state(
                    codex_home=target_home,
                    shared_root=shared,
                    project_registry=registry,
                    source_machines=["source-machine"],
                    current_machine="target-machine",
                    yes=True,
                )

            imported = target_home / source_session.relative_to(source_home)
            imported.write_bytes(b"local divergent session\n")
            with patch("agent_bridge.state_sync._codex_desktop_running", return_value=False):
                result = apply_codex_state(
                    codex_home=target_home,
                    shared_root=shared,
                    project_registry=registry,
                    source_machines=["source-machine"],
                    current_machine="target-machine",
                    yes=True,
                )
            self.assertEqual(result["artifact_results"]["conflict"], 1)
            self.assertEqual(imported.read_bytes(), b"local divergent session\n")
            conflicts = list((target_home / "session-sync-conflicts" / "source-machine").rglob("*.jsonl"))
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].read_bytes(), source_session.read_bytes())

    def test_missing_source_artifact_remains_cataloged_but_is_not_auto_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            source_home, _, session, registry = self._source_fixture(root)
            first = publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            session.unlink()
            second = publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            self.assertEqual(second["retained_artifact_count"], 1)
            self.assertEqual(second["active_artifact_count"], 0)
            self.assertEqual(second["new_objects"], 0)
            self.assertEqual(first["stored_bytes"], second["stored_bytes"])

    def test_status_and_launch_agent_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            source_home, _, _, registry = self._source_fixture(root)
            publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            rows = list_state_sync_sources(shared_root=shared, current_machine="target-machine")
            self.assertEqual([row["machine_id"] for row in rows], ["source-machine"])
            self.assertFalse(rows[0]["current_machine"])

            body = _macos_plist(
                arguments=["/usr/local/bin/agent", "code", "state-sync", "publish"],
                interval_seconds=60,
                stdout_path=root / "stdout.log",
                stderr_path=root / "stderr.log",
            )
            value = plistlib.loads(body)
            self.assertEqual(value["Label"], SCHEDULER_LABEL)
            self.assertEqual(value["StartInterval"], 300)
            self.assertEqual(value["ProgramArguments"][2:4], ["state-sync", "publish"])
            self.assertIn(str(Path(os.sys.executable).resolve().parent), value["EnvironmentVariables"]["PATH"])

            arguments = _scheduler_arguments(
                shared_root=root / "SharedAgentData",
                codex_home=root / ".codex",
                pull=True,
                machine_id="stable-machine",
                project_registry=registry,
                source_machines=["source-machine"],
                path_maps=["/source=/target"],
            )
            self.assertEqual(arguments[arguments.index("--machine-id") + 1], "stable-machine")
            self.assertEqual(arguments[arguments.index("--project-registry") + 1], str(registry))
            self.assertEqual(arguments[arguments.index("--from-machine") + 1], "source-machine")
            self.assertEqual(arguments[arguments.index("--path-map") + 1], "/source=/target")
            self.assertIn("--defer-if-running", arguments)
            self.assertIn("--quiet", arguments)

    def test_atomic_metadata_replace_retries_transient_cloud_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "manifest.json"
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source: str | bytes | os.PathLike[str] | os.PathLike[bytes], target: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(errno.EPERM, "temporary cloud lock")
                real_replace(source, target)

            with patch("agent_bridge.state_sync.os.replace", side_effect=flaky_replace), patch(
                "agent_bridge.state_sync.time.sleep"
            ):
                _atomic_write_bytes(destination, b"ok\n")
            self.assertEqual(destination.read_bytes(), b"ok\n")
            self.assertEqual(attempts, 3)
            self.assertEqual(list(destination.parent.glob(".*.tmp")), [])

    def test_session_index_merge_preserves_unrelated_lines_duplicates_and_future_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "session_index.jsonl"
            original = (
                b'{"id":"existing","thread_name":"Local","updated_at":"2030-01-01T00:00:00Z","future":{"keep":true}}\n'
                b'not valid json\n'
                b'["non-object", 1]\n'
                b'{"id":"existing","thread_name":"Older duplicate","updated_at":"2029-01-01T00:00:00Z"}\n'
                b'{"id":"local-only","thread_name":"Keep me","updated_at":"2032-01-01T00:00:00Z"}\n'
            )
            path.write_bytes(original)

            added = _merge_session_index(
                home,
                [
                    {
                        "thread_id": "existing",
                        "thread": {"id": "existing", "title": "Remote newer", "updated_at_ms": 1_925_000_000_000},
                    },
                    {
                        "thread_id": "remote-only",
                        "thread": {"id": "remote-only", "title": "Imported", "updated_at_ms": 1_925_000_000_000},
                    },
                ],
            )

            self.assertEqual(added, 1)
            lines = path.read_bytes().splitlines(keepends=True)
            self.assertEqual(lines[1:5], original.splitlines(keepends=True)[1:5])
            updated = json.loads(lines[0])
            self.assertEqual(updated["thread_name"], "Remote newer")
            self.assertEqual(updated["future"], {"keep": True})
            self.assertNotIn("updated_at_ms", updated)
            self.assertEqual(json.loads(lines[-1])["id"], "remote-only")

    def test_same_size_session_rewrite_is_rechunked_not_treated_as_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            source_home, _, session, registry = self._source_fixture(root)
            publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            original_size = session.stat().st_size
            session.write_bytes((b"y" * CHUNK_SIZE) + b'{"type":"user","text":"first"}\n')
            self.assertEqual(session.stat().st_size, original_size)

            rewritten = publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )

            # The unchanged tail chunk can still deduplicate, but a same-size
            # rewrite must not reuse the old first chunk as an append prefix.
            self.assertEqual(rewritten["reused_objects"], 1)
            self.assertGreaterEqual(rewritten["new_objects"], 1)
            self.assertGreater(rewritten["new_stored_bytes"], 0)

    def test_publisher_lock_refuses_concurrent_metadata_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}, clear=False
        ):
            with _publisher_lock("same-machine"):
                with self.assertRaises(StateSyncError):
                    with _publisher_lock("same-machine"):
                        pass

    def test_pid_liveness_check_never_uses_os_kill_on_windows(self) -> None:
        with patch("agent_bridge.state_sync.os.kill") as kill:
            self.assertTrue(_pid_is_running(os.getpid()))
        if os.name == "nt":
            kill.assert_not_called()
        else:
            kill.assert_called_once_with(os.getpid(), 0)

    def test_pid_liveness_check_releases_dead_process(self) -> None:
        process = subprocess.Popen([os.sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        self.assertFalse(_pid_is_running(process.pid))

    def test_apply_lock_refuses_concurrent_native_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}, clear=False
        ):
            with _apply_lock("same-machine"):
                with self.assertRaises(StateSyncError):
                    with _apply_lock("same-machine"):
                        pass

    def test_native_state_read_failure_is_not_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".codex-global-state.json"
            state.write_text('{"keep": true}', encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=PermissionError("temporarily locked")):
                with self.assertRaisesRegex(StateSyncError, "could not read native Codex state"):
                    _read_native_json_object(state)
            self.assertEqual(state.read_text(encoding="utf-8"), '{"keep": true}')

    def test_artifact_relative_path_rejects_mid_path_drive_and_ads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in ("sessions/D:/escape.jsonl", "sessions/name:stream.jsonl"):
                status, destination = _install_artifact(
                    archive=root / "archive",
                    codex_home=root / "codex",
                    source_machine="source-machine",
                    row={"relative_path": relative},
                    backup_root=root / "backup",
                )
                self.assertEqual((status, destination), ("invalid", ""))

    def test_missing_project_path_blocks_apply_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            source_home, _, _, registry = self._source_fixture(root)
            target_home, _ = self._target_fixture(root)
            value = json.loads(registry.read_text(encoding="utf-8"))
            missing = root / "missing-target-project"
            value["projects"][0]["workspace_windows"] = str(missing)
            registry.write_text(json.dumps(value), encoding="utf-8")
            publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            preview = apply_codex_state(
                codex_home=target_home,
                shared_root=shared,
                project_registry=registry,
                source_machines=["source-machine"],
                current_machine="target-machine",
                platform_name="windows",
                dry_run=True,
            )
            self.assertEqual(preview["missing_project_paths"], [str(missing)])
            with patch("agent_bridge.state_sync._codex_desktop_running", return_value=False):
                with self.assertRaisesRegex(StateSyncError, "invalid sidebar project roots"):
                    apply_codex_state(
                        codex_home=target_home,
                        shared_root=shared,
                        project_registry=registry,
                        source_machines=["source-machine"],
                        current_machine="target-machine",
                        platform_name="windows",
                        yes=True,
                    )
            self.assertFalse((target_home / "backups").exists())

    def test_apply_rechecks_desktop_before_mutation_and_defers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            source_home, _, _, registry = self._source_fixture(root)
            target_home, _ = self._target_fixture(root)
            publish_codex_state(
                codex_home=source_home,
                shared_root=shared,
                project_registry=registry,
                machine_id="source-machine",
                settle_seconds=0,
            )
            with patch("agent_bridge.state_sync._codex_desktop_running", side_effect=[False, True]):
                result = apply_codex_state(
                    codex_home=target_home,
                    shared_root=shared,
                    project_registry=registry,
                    source_machines=["source-machine"],
                    current_machine="target-machine",
                    platform_name="windows",
                    yes=True,
                    defer_if_running=True,
                )
            self.assertEqual(result["status"], "deferred_codex_running")
            self.assertTrue((target_home / ".local-state-sync-pending.json").is_file())
            self.assertFalse((target_home / "backups").exists())

    def test_no_remote_source_keeps_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "SharedAgentData"
            shared.mkdir()
            home = root / "codex"
            home.mkdir()
            pending = home / ".local-state-sync-pending.json"
            pending.write_text('{"kind":"codex_state_sync_pending_apply"}', encoding="utf-8")
            result = apply_codex_state(
                codex_home=home,
                shared_root=shared,
                current_machine="target-machine",
                platform_name="windows",
                yes=True,
            )
            self.assertEqual(result["status"], "no_remote_sources")
            self.assertTrue(pending.is_file())

    def test_powershell_scheduler_literals_escape_metacharacters(self) -> None:
        self.assertEqual(_powershell_literal("C:/A&B/O'Brien/agent.cmd"), "'C:/A&B/O''Brien/agent.cmd'")
        body = _windows_scheduler_script(
            ["C:/A&B/O'Brien/agent.cmd", "code", "state-sync", "sync"],
            Path("C:/logs/state-sync.log"),
        ).decode("utf-8")
        self.assertIn("$ErrorActionPreference = 'Continue'", body)
        self.assertIn("& $agent 'code' 'state-sync' 'sync'", body)
        self.assertIn("exit $exitCode", body)

    def test_desktop_detection_covers_current_macos_and_windows_process_names(self) -> None:
        with patch(
            "agent_bridge.state_sync.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
        ):
            self.assertTrue(_codex_desktop_running("macos"))
        with patch(
            "agent_bridge.state_sync.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout='"ChatGPT.exe","123","Console","1","10,000 K"\n',
                stderr="",
            ),
        ):
            self.assertTrue(_codex_desktop_running("windows"))
        with patch("agent_bridge.state_sync.subprocess.run", side_effect=OSError("tasklist blocked")):
            self.assertTrue(_codex_desktop_running("windows"))
        with patch(
            "agent_bridge.state_sync.subprocess.run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="denied"),
        ):
            self.assertTrue(_codex_desktop_running("windows"))


if __name__ == "__main__":
    unittest.main()
