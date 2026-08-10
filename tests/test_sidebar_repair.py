from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_bridge import sidebar_repair


def _live_state() -> dict:
    return {
        "local-projects": {
            "bad": {"name": "foreign", "rootPaths": ["/Users/tts/Code/foreign"]},
        },
        "project-order": ["bad"],
        "electron-saved-workspace-roots": ["/Users/tts/Code/foreign"],
        "active-workspace-roots": ["/Users/tts/Code/foreign"],
        "electron-workspace-root-labels": {"/Users/tts/Code/foreign": "foreign"},
        "sidebar-project-thread-orders": {"bad": ["thread-imported"]},
        "thread-project-assignments": {"thread-imported": {"projectId": "bad", "cwd": "/Users/tts/Code/foreign"}},
        "thread-workspace-root-hints": {"thread-imported": "/Users/tts/Code/foreign"},
        "projectless-thread-ids": ["new-native-thread"],
        "thread-projectless-output-directories": {"new-native-thread": "C:\\safe"},
        "prompt-history": ["must stay"],
        "unrelated": {"must": "stay"},
        "electron-persisted-atom-state": {
            "sidebar-project-expanded-v1-codex:bad": True,
            "unified-sidebar-project-order-v1": ["codex:project:bad"],
            "sidebar-collapsed-groups": {"/Users/tts/Code/foreign": True},
            'composer-mode-by-project:["local","/Users/tts/Code/foreign"]': "agent",
            'composer-mode-by-project:["local","/home/tts/code/foreign"]': "agent",
            "thread-workspace-state-v1:new-native-thread": {"mode": "local"},
            "sidebar-width": 321,
        },
    }


def _source_state(project_root: Path) -> dict:
    return {
        "local-projects": {
            "good": {"name": "native", "rootPaths": [str(project_root)]},
        },
        "project-order": ["good"],
        "selected-project": {"projectKind": "local", "projectId": "good"},
        "thread-project-assignments": {"old-thread": {"projectId": "good", "cwd": str(project_root)}},
        "thread-workspace-root-hints": {"old-thread": str(project_root)},
        "electron-persisted-atom-state": {
            "flat-project-sidebar-preferences-v1": {"projectSortMode": "priority"},
            "sidebar-project-expanded-v1-codex:good": True,
        },
    }


class SidebarRepairProjectionTests(unittest.TestCase):
    def test_projection_is_field_scoped(self) -> None:
        source = {
            "local-projects": {"good": {"name": "native", "rootPaths": [r"C:\native"]}},
            "project-order": ["good"],
            "thread-project-assignments": {},
            "thread-workspace-root-hints": {},
            "electron-persisted-atom-state": {
                "sidebar-project-expanded-v1-codex:good": True,
            },
        }
        repaired, details = sidebar_repair._repair_projection(_live_state(), source)
        self.assertEqual(list(repaired["local-projects"]), ["good"])
        self.assertNotIn("electron-saved-workspace-roots", repaired)
        self.assertEqual(repaired["projectless-thread-ids"], ["new-native-thread"])
        self.assertEqual(repaired["prompt-history"], ["must stay"])
        atom = repaired["electron-persisted-atom-state"]
        self.assertNotIn("sidebar-project-expanded-v1-codex:bad", atom)
        self.assertNotIn("unified-sidebar-project-order-v1", atom)
        self.assertNotIn('composer-mode-by-project:["local","/home/tts/code/foreign"]', atom)
        self.assertIn("sidebar-project-expanded-v1-codex:good", atom)
        self.assertIn("thread-workspace-state-v1:new-native-thread", atom)
        self.assertEqual(atom["sidebar-width"], 321)
        self.assertGreater(details["atom_removed_count"], 0)

    def test_process_guard_includes_codex_package_host(self) -> None:
        self.assertIn("chatgpt.exe", sidebar_repair.CODEX_PROCESS_NAMES)

    def test_projection_does_not_create_empty_atom_state(self) -> None:
        live = {"local-projects": {"good": {"name": "good", "rootPaths": [r"C:\good"]}}}
        repaired, _ = sidebar_repair._repair_projection(live, live)
        self.assertNotIn("electron-persisted-atom-state", repaired)

    def test_latest_backup_is_selected_by_timestamped_directory_not_file_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            older = home / "backups" / "sidebar-state-sync-20260101T000000Z" / ".codex-global-state.json"
            newer = home / "backups" / "sidebar-state-sync-20260102T000000Z" / ".codex-global-state.json"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.write_text("{}", encoding="utf-8")
            newer.write_text("{}", encoding="utf-8")
            os.utime(older, (2_000_000_000, 2_000_000_000))
            os.utime(newer, (1_000_000_000, 1_000_000_000))
            selected = sidebar_repair._find_source(home)
        self.assertEqual(selected, newer.resolve())


@unittest.skipUnless(os.name == "nt", "offline sidebar application is Windows-native")
class SidebarRepairWindowsTests(unittest.TestCase):
    def test_windows_projection_maps_foreign_roots_and_drops_unavailable_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            windows_home = base / "user"
            code_root = windows_home / "Code" / "agent-bridge"
            career_root = windows_home / "OneDrive - nextcz.com" / "SharedProjects" / "Career Transition"
            code_root.mkdir(parents=True)
            (code_root / ".git").mkdir()
            career_root.mkdir(parents=True)
            live = _live_state()
            live["local-projects"] = {
                "bridge": {
                    "name": "agent-bridge",
                    "rootPaths": [r"\\wsl.localhost\Ubuntu\home\tts\code\agent-bridge"],
                    "lastUsedWorktree": "/home/tts/.codex/worktrees/62f3/agent-bridge",
                },
                "career": {
                    "name": "career-transition",
                    "rootPaths": [
                        "/Users/tts/Library/CloudStorage/OneDrive-nextcz.com/SharedProjects/Career Transition"
                    ],
                },
                "missing": {"name": "mac-only", "rootPaths": ["/Users/tts/Code/mac-only"]},
                "g-p-test": {
                    "name": "ChatGPT project",
                    "rootPaths": ["/Users/tts/.codex/.chatgpt-projects/g-p-test"],
                },
            }
            live["project-order"] = ["bridge", "career", "missing", "g-p-test"]
            live["thread-project-assignments"] = {
                "thread-bridge": {
                    "projectId": "bridge",
                    "cwd": "/home/tts/.codex/worktrees/62f3/agent-bridge",
                },
                "thread-missing": {"projectId": "missing", "cwd": "/Users/tts/Code/mac-only"},
            }
            live["thread-workspace-root-hints"] = {
                "thread-bridge": "/home/tts/.codex/worktrees/62f3/agent-bridge",
                "thread-missing": "/Users/tts/Code/mac-only",
            }
            resolver = sidebar_repair._WindowsPathResolver(windows_home=windows_home, registry={"projects": []})
            source, details = sidebar_repair._build_windows_projection(live, resolver=resolver)
            mounted = "/mnt/" + code_root.drive[0].lower() + "/" + code_root.as_posix()[3:]
            mounted_resolved = resolver.resolve(mounted, project_name="agent-bridge")

        self.assertEqual(source["project-order"], ["bridge", "career"])
        self.assertEqual(source["local-projects"]["bridge"]["rootPaths"], [str(code_root)])
        self.assertEqual(source["local-projects"]["bridge"]["lastUsedWorktree"], str(code_root))
        self.assertEqual(source["local-projects"]["career"]["rootPaths"], [str(career_root)])
        self.assertEqual(source["thread-project-assignments"]["thread-bridge"]["cwd"], str(code_root))
        self.assertNotIn("thread-missing", source["thread-project-assignments"])
        self.assertEqual(details["mapped_project_count"], 2)
        self.assertEqual(details["dropped_project_count"], 2)

        self.assertEqual(mounted_resolved, str(code_root))

    def test_source_validation_rejects_residual_foreign_paths_in_nested_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "native-project"
            project.mkdir()
            source = _source_state(project)
            source["local-projects"]["good"]["lastUsedWorktree"] = "/Users/tts/.codex/worktrees/1234/native"
            _, issues = sidebar_repair._source_projects(source)
        self.assertTrue(any("residual foreign runtime path" in issue for issue in issues))

    def test_source_validation_ignores_foreign_paths_outside_restored_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "native-project"
            project.mkdir()
            source = _source_state(project)
            source["prompt-history"] = ["/Users/tts/this-field-is-not-restored"]
            _, issues = sidebar_repair._source_projects(source)
        self.assertFalse(any("residual foreign runtime path" in issue for issue in issues))

    def test_source_validation_rejects_foreign_assignment_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "native-project"
            project.mkdir()
            source = _source_state(project)
            source["thread-project-assignments"]["old-thread"]["cwd"] = "/Users/tts/Code/foreign"
            _, issues = sidebar_repair._source_projects(source)
        self.assertTrue(any("non-Windows cwd" in issue for issue in issues))

    def test_stage_then_apply_pending_preserves_native_store_and_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".codex"
            home.mkdir()
            project = base / "native-project"
            project.mkdir()
            live_path = home / ".codex-global-state.json"
            source_path = home / "backups" / "sidebar-state-sync-test" / ".codex-global-state.json"
            source_path.parent.mkdir(parents=True)
            live_path.write_text(json.dumps(_live_state()), encoding="utf-8")
            source_path.write_text(json.dumps(_source_state(project)), encoding="utf-8")
            (home / "state_5.sqlite").write_bytes(b"native-db-unchanged")
            with (
                mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(base / "state")}, clear=False),
                mock.patch.object(sidebar_repair, "_codex_processes", return_value=[]),
            ):
                staged = sidebar_repair.stage_sidebar_repair(codex_home=home, source=source_path)
                self.assertTrue(Path(staged["pending"]).is_file())
                result = sidebar_repair.apply_pending_sidebar_repair()
            self.assertEqual(result["status"], "applied")
            self.assertTrue(Path(result["backup"]).is_file())
            repaired = json.loads(live_path.read_text(encoding="utf-8"))
            companion_path = live_path.with_name(f"{live_path.name}.bak")
            self.assertEqual(list(repaired["local-projects"]), ["good"])
            self.assertEqual(json.loads(companion_path.read_text(encoding="utf-8")), repaired)
            self.assertEqual(repaired["projectless-thread-ids"], ["new-native-thread"])
            self.assertEqual(repaired["prompt-history"], ["must stay"])
            self.assertEqual((home / "state_5.sqlite").read_bytes(), b"native-db-unchanged")
            self.assertFalse(Path(staged["pending"]).exists())

    def test_apply_pending_refuses_live_state_drift_after_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".codex"
            home.mkdir()
            project = base / "native-project"
            project.mkdir()
            live_path = home / ".codex-global-state.json"
            source_path = base / "source.json"
            live_path.write_text(json.dumps(_live_state()), encoding="utf-8")
            source_path.write_text(json.dumps(_source_state(project)), encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(base / "state")}, clear=False),
                mock.patch.object(sidebar_repair, "_codex_processes", return_value=[]),
            ):
                staged = sidebar_repair.stage_sidebar_repair(codex_home=home, source=source_path)
                changed = _live_state()
                changed["new-sidebar-change"] = True
                live_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(sidebar_repair.SidebarRepairError, "changed after staging"):
                    sidebar_repair.apply_pending_sidebar_repair()
            self.assertTrue(Path(staged["pending"]).exists())
            self.assertTrue(json.loads(live_path.read_text(encoding="utf-8"))["new-sidebar-change"])

    def test_apply_windows_path_repair_builds_from_final_offline_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".codex"
            home.mkdir()
            project = base / "native-project"
            project.mkdir()
            live = _live_state()
            live["local-projects"] = {
                "good": {"name": "native", "rootPaths": ["/Users/tts/Code/native-project"]}
            }
            live["project-order"] = ["good"]
            live["thread-project-assignments"] = {
                "thread": {"projectId": "good", "cwd": "/Users/tts/Code/native-project"}
            }
            live["thread-workspace-root-hints"] = {"thread": "/Users/tts/Code/native-project"}
            live_path = home / ".codex-global-state.json"
            live_path.write_text(json.dumps(live), encoding="utf-8")
            registry_path = base / "projects.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "name": "native",
                                "slug": "native",
                                "workspace_macos": "/Users/tts/Code/native-project",
                                "workspace_windows": str(project),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(base / "state")}, clear=False),
                mock.patch.object(sidebar_repair, "_codex_processes", return_value=[]),
            ):
                result = sidebar_repair.apply_windows_path_repair(
                    codex_home=home,
                    project_registry=registry_path,
                    windows_home=base / "user",
                )
            repaired = json.loads(live_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "applied")
            self.assertEqual(repaired["local-projects"]["good"]["rootPaths"], [str(project)])
            self.assertEqual(repaired["thread-project-assignments"]["thread"]["cwd"], str(project))

    def test_apply_refuses_while_codex_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".codex"
            home.mkdir()
            project = base / "native-project"
            project.mkdir()
            live_path = home / ".codex-global-state.json"
            companion_path = live_path.with_name(f"{live_path.name}.bak")
            source_path = base / "source.json"
            original = json.dumps(_live_state())
            companion_original = json.dumps({**_live_state(), "companion": True})
            live_path.write_text(original, encoding="utf-8")
            companion_path.write_text(companion_original, encoding="utf-8")
            source_path.write_text(json.dumps(_source_state(project)), encoding="utf-8")
            with mock.patch.object(
                sidebar_repair,
                "_codex_processes",
                return_value=[{"name": "codex.exe", "pid": 123}],
            ):
                with self.assertRaisesRegex(sidebar_repair.SidebarRepairError, "close Codex"):
                    sidebar_repair.apply_sidebar_repair(codex_home=home, source=source_path)
            self.assertEqual(live_path.read_text(encoding="utf-8"), original)
            self.assertEqual(companion_path.read_text(encoding="utf-8"), companion_original)

    def test_bare_apply_with_explicit_source_cannot_bypass_pending_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.dict(
                os.environ,
                {"AGENT_BRIDGE_STATE_DIR": str(base / "state")},
                clear=False,
            ):
                pending = sidebar_repair._pending_path()
                pending.parent.mkdir(parents=True)
                pending.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(sidebar_repair.SidebarRepairError, "staged sidebar repair exists"):
                    sidebar_repair.sidebar_repair_cmd(["apply", "--source", str(base / "source.json")])

    def test_post_write_validation_failure_rolls_back_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".codex"
            home.mkdir()
            project = base / "native-project"
            project.mkdir()
            live_path = home / ".codex-global-state.json"
            companion_path = live_path.with_name(f"{live_path.name}.bak")
            source_path = base / "source.json"
            original = json.dumps(_live_state())
            companion_original = json.dumps({**_live_state(), "companion": True})
            live_path.write_text(original, encoding="utf-8")
            companion_path.write_text(companion_original, encoding="utf-8")
            source_path.write_text(json.dumps(_source_state(project)), encoding="utf-8")
            rows = [{"project_id": "good", "name": "native", "root_paths": [str(project)]}]
            with (
                mock.patch.object(sidebar_repair, "_codex_processes", return_value=[]),
                mock.patch.object(
                    sidebar_repair,
                    "_source_projects",
                    side_effect=[(rows, []), (rows, ["forced post-write failure"])],
                ),
            ):
                with self.assertRaisesRegex(sidebar_repair.SidebarRepairError, "was restored"):
                    sidebar_repair.apply_sidebar_repair(codex_home=home, source=source_path)
            self.assertEqual(live_path.read_text(encoding="utf-8"), original)
            self.assertEqual(companion_path.read_text(encoding="utf-8"), companion_original)

    def test_post_write_failure_removes_new_companion_file_during_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / ".codex"
            home.mkdir()
            project = base / "native-project"
            project.mkdir()
            live_path = home / ".codex-global-state.json"
            companion_path = live_path.with_name(f"{live_path.name}.bak")
            source_path = base / "source.json"
            original = json.dumps(_live_state())
            live_path.write_text(original, encoding="utf-8")
            source_path.write_text(json.dumps(_source_state(project)), encoding="utf-8")
            rows = [{"project_id": "good", "name": "native", "root_paths": [str(project)]}]
            with (
                mock.patch.object(sidebar_repair, "_codex_processes", return_value=[]),
                mock.patch.object(
                    sidebar_repair,
                    "_source_projects",
                    side_effect=[(rows, []), (rows, ["forced post-write failure"])],
                ),
            ):
                with self.assertRaisesRegex(sidebar_repair.SidebarRepairError, "was restored"):
                    sidebar_repair.apply_sidebar_repair(codex_home=home, source=source_path)
            self.assertEqual(live_path.read_text(encoding="utf-8"), original)
            self.assertFalse(companion_path.exists())


if __name__ == "__main__":
    unittest.main()
