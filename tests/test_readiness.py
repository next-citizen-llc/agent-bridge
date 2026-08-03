from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_bridge import readiness
from agent_bridge.context_adapters import ContextAdapterError, context_status, install_context_adapters
from agent_bridge.readiness import (
    _auth_check,
    _github_check,
    _mcp_health_checks,
    _ollama_check,
    _overall,
    _sanitize_detail,
    aggregate_readiness,
    publish_readiness,
    redacted_summary,
    resolve_shared_roots,
    run_preflight,
    shared_root_candidates,
    validate_readiness_report,
)


class ReadinessTests(unittest.TestCase):
    def test_unknown_advisory_does_not_degrade_operational_readiness(self) -> None:
        checks = [
            {"name": "client_binary", "required": True, "status": "ready"},
            {"name": "mcp_health", "required": False, "status": "unknown"},
        ]
        self.assertEqual(_overall(checks), "ready")

    def test_advisory_failure_still_degrades_operational_readiness(self) -> None:
        checks = [
            {"name": "client_binary", "required": True, "status": "ready"},
            {"name": "mcp_health", "required": False, "status": "degraded"},
        ]
        self.assertEqual(_overall(checks), "degraded")

    def test_auth_success_exit_with_logged_out_message_is_blocked(self) -> None:
        with mock.patch("agent_bridge.readiness._run", return_value=(0, "Not logged in · Please run /login")):
            result = _auth_check("codex", project_dir=Path.cwd(), timeout=1)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_class"], "auth_missing")

    def test_authenticated_success_text_is_not_a_false_positive(self) -> None:
        with mock.patch("agent_bridge.readiness._run", return_value=(0, "Authenticated as user@example.test")):
            result = _auth_check("codex", project_dir=Path.cwd(), timeout=1)
        self.assertEqual(result["status"], "ready")

    def test_dns_failure_is_degraded_not_auth_missing(self) -> None:
        with mock.patch("agent_bridge.readiness._run", return_value=(1, "Could not resolve host: api.example.test")):
            result = _auth_check("codex", project_dir=Path.cwd(), timeout=1)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_class"], "dns_failure")

    def test_probe_timeout_is_degraded_not_auth_missing(self) -> None:
        with mock.patch("agent_bridge.readiness._run", return_value=(124, "timed out after 1s")):
            result = _auth_check("grok", project_dir=Path.cwd(), timeout=1)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error_class"], "network_unreachable")

    def test_authorization_header_is_fully_redacted(self) -> None:
        detail = _sanitize_detail("Authorization: Bearer top-secret-value")
        self.assertEqual(detail, "[redacted]")

    def test_github_wrong_identity_is_blocked(self) -> None:
        with mock.patch("agent_bridge.readiness.shutil.which", return_value="/usr/bin/gh"):
            with mock.patch("agent_bridge.readiness._run", side_effect=[(0, "logged in"), (0, "wrong-user")]):
                result = _github_check(project_dir=Path.cwd(), timeout=1, expected_login="expected-user", required=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_class"], "auth_wrong_identity")

    def test_ollama_probe_rejects_non_loopback_host(self) -> None:
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "http://192.0.2.10:11434"}, clear=False):
            result = _ollama_check(1)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_class"], "permission_denied")

    def test_missing_shared_roots_are_not_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": tmp,
                "PATH": "/usr/bin:/bin",
                "AGENT_BRIDGE_ROOTS_CONFIG": str(Path(tmp) / "missing-roots.json"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                report = resolve_shared_roots()
        self.assertFalse(report["ok"])
        self.assertTrue(all(not row["exists"] for row in report["roots"].values()))

    def test_explicit_split_roots_avoid_ambiguous_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            skills = base / "personal" / "SharedAgentSkills"
            data = base / "business" / "SharedAgentData"
            conversations = base / "personal" / "SharedAgentConversations"
            for path in (skills, data, conversations):
                path.mkdir(parents=True)
            env = {
                "HOME": str(base / "home"),
                "AGENT_BRIDGE_SHARED_SKILLS_ROOT": str(skills),
                "AGENT_BRIDGE_SHARED_DATA_ROOT": str(data),
                "AGENT_BRIDGE_SHARED_CONVERSATIONS_ROOT": str(conversations),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                report = resolve_shared_roots()
        self.assertTrue(report["ok"])
        self.assertEqual(report["roots"]["skills"]["selected"], str(skills))
        self.assertEqual(report["roots"]["data"]["selected"], str(data))
        self.assertEqual(report["roots"]["conversations"]["selected"], str(conversations))

    def test_multiple_discovered_accounts_are_reported_as_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for account in ("OneDrive-Personal", "OneDrive-Work"):
                for leaf in ("SharedAgentSkills", "SharedAgentData", "SharedAgentConversations"):
                    (home / "Library/CloudStorage" / account / leaf).mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(home), "AGENT_BRIDGE_ROOTS_CONFIG": str(home / "none.json")}, clear=True):
                report = resolve_shared_roots()
        self.assertFalse(report["ok"])
        self.assertEqual({row["kind"] for row in report["conflicts"]}, {"skills", "data", "conversations"})

    def test_windows_onedrive_environment_is_a_root_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"OneDriveCommercial": tmp}, clear=True):
                candidates = shared_root_candidates("data")
        self.assertIn(Path(tmp) / "SharedAgentData", candidates)

    def test_preflight_uses_typed_failure_and_redaction_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "PATH": "", "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            with mock.patch.dict(os.environ, env, clear=False):
                report = run_preflight("missing-client", "cli", scope="session", project_dir=Path(tmp))
                summary = redacted_summary(report)
        self.assertEqual(report["overall"], "blocked")
        binary = next(row for row in report["checks"] if row["name"] == "client_binary")
        self.assertEqual(binary["error_class"], "config_missing")
        self.assertNotIn("detail", summary["checks"][0])
        self.assertNotIn("project_dir", summary)
        self.assertEqual(validate_readiness_report(report), [])

    def test_session_preflight_is_bounded_and_does_not_run_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "PATH": "", "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            started = time.monotonic()
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("agent_bridge.readiness._run") as run:
                    run_preflight("codex", "cli", scope="session", project_dir=Path(tmp))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        run.assert_not_called()

    def test_gui_health_does_not_inherit_cli_auth_or_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "PATH": "", "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("agent_bridge.readiness._auth_check") as auth:
                    with mock.patch("agent_bridge.readiness._mcp_health_checks") as mcp:
                        report = run_preflight("grok", "gui", scope="work", project_dir=Path(tmp))
        auth.assert_not_called()
        mcp.assert_not_called()
        self.assertEqual(next(row for row in report["checks"] if row["name"] == "gui_auth")["status"], "unknown")
        self.assertEqual(next(row for row in report["checks"] if row["name"] == "mcp_health")["status"], "unknown")

    def test_codex_mcp_health_is_scoped_per_server(self) -> None:
        payload = json.dumps(
            [
                {"name": "github", "enabled": True, "auth_status": "logged_in"},
                {"name": "mailbox", "enabled": True, "auth_status": "not_logged_in"},
            ]
        )
        with mock.patch("agent_bridge.readiness._run", return_value=(0, payload)):
            rows = _mcp_health_checks("codex", project_dir=Path.cwd(), timeout=1)
        self.assertEqual({row["name"] for row in rows}, {"mcp:github", "mcp:mailbox"})
        self.assertEqual(next(row for row in rows if row["name"] == "mcp:mailbox")["error_class"], "mcp_unauthed")

    def test_codex_mcp_missing_stdio_command_is_degraded(self) -> None:
        payload = json.dumps(
            [
                {
                    "name": "missing-server",
                    "enabled": True,
                    "auth_status": "unsupported",
                    "transport": {"type": "stdio", "command": "/definitely/missing/mcp-server"},
                }
            ]
        )
        with mock.patch("agent_bridge.readiness._run", return_value=(0, payload)):
            rows = _mcp_health_checks("codex", project_dir=Path.cwd(), timeout=1)
        self.assertEqual(rows[0]["name"], "mcp:missing-server")
        self.assertEqual(rows[0]["status"], "degraded")
        self.assertEqual(rows[0]["error_class"], "source_unreachable")
        self.assertIn("codex mcp get", rows[0]["repair_command"])

    def test_publish_is_redacted_deduplicated_and_aggregatable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shared"
            report = {
                "schema_version": "1.0",
                "kind": "agent-bridge.readiness",
                "scope": "work",
                "generated_at": "2030-01-01T00:00:00Z",
                "expires_at": "2030-01-01T00:15:00Z",
                "machine_id": "machine-a",
                "client": "codex",
                "surface": "cli",
                "project_dir": "/private/project",
                "overall": "ready",
                "checks": [{"name": "github_auth", "status": "ready", "required": True, "error_class": "", "detail": "token=secret", "repair_command": ""}],
                "shared_roots": {},
            }
            first = publish_readiness(report, data_root=str(root))
            second = publish_readiness(report, data_root=str(root))
            event_lines = Path(first["event_file"]).read_text(encoding="utf-8").splitlines()
            aggregate = aggregate_readiness(data_root=str(root))
            published_text = Path(first["published_file"]).read_text(encoding="utf-8")
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(len(event_lines), 1)
        self.assertEqual(len(aggregate["rows"]), 1)
        self.assertNotIn("private/project", published_text)
        self.assertNotIn("secret", published_text)

    def test_context_adapter_is_idempotent_and_preserves_manual_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "policy.md").write_text("Always verify live state.\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("# Manual preface\n", encoding="utf-8")
            manifest = root / "context.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "modules": [{"id": "policy", "path": "policy.md"}],
                        "adapters": [{"client": "codex", "path": "AGENTS.md", "modules": ["policy"]}],
                    }
                ),
                encoding="utf-8",
            )
            first = install_context_adapters(manifest)
            installed = (root / "AGENTS.md").read_text(encoding="utf-8")
            second = install_context_adapters(manifest)
            status = context_status(manifest)
        self.assertTrue(first["ok"])
        self.assertTrue(first["adapters"][0]["changed"])
        self.assertTrue(installed.startswith("# Manual preface"))
        self.assertFalse(second["adapters"][0]["changed"])
        self.assertTrue(status["ok"])

    def test_context_status_reports_partial_marker_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "policy.md").write_text("Policy\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("<!-- BEGIN agent-bridge generated context -->\n", encoding="utf-8")
            manifest = root / "context.json"
            manifest.write_text(
                json.dumps(
                    {
                        "modules": [{"id": "policy", "path": "policy.md"}],
                        "adapters": [{"client": "codex", "path": "AGENTS.md", "modules": ["policy"]}],
                    }
                ),
                encoding="utf-8",
            )
            status = context_status(manifest)
        self.assertFalse(status["ok"])
        self.assertEqual(status["adapters"][0]["status"], "conflict")
        self.assertEqual(status["adapters"][0]["error_class"], "context_stale")

    def test_context_status_reports_reversed_marker_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "policy.md").write_text("Canonical policy text.\n", encoding="utf-8")
            (root / "AGENTS.md").write_text(
                "<!-- END agent-bridge generated context -->\n<!-- BEGIN agent-bridge generated context -->\n",
                encoding="utf-8",
            )
            manifest = root / "context.json"
            manifest.write_text(json.dumps({"modules": [{"id": "policy", "path": "policy.md"}], "adapters": [{"client": "codex", "path": "AGENTS.md", "modules": ["policy"]}]}), encoding="utf-8")
            status = context_status(manifest)
        self.assertFalse(status["ok"])
        self.assertEqual(status["adapters"][0]["status"], "conflict")
        self.assertIn("reversed", status["adapters"][0]["error"])

    def test_context_update_requires_explicit_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "policy.md"
            module.write_text("Original canonical policy.\n", encoding="utf-8")
            manifest = root / "context.json"
            manifest.write_text(json.dumps({"modules": [{"id": "policy", "path": "policy.md"}], "adapters": [{"client": "grok", "path": "GROK.md", "modules": ["policy"]}]}), encoding="utf-8")
            install_context_adapters(manifest)
            module.write_text("Updated canonical policy.\n", encoding="utf-8")
            with self.assertRaises(ContextAdapterError):
                install_context_adapters(manifest)
            result = install_context_adapters(manifest, force=True)
        self.assertTrue(result["adapters"][0]["changed"])

    def test_context_overlap_reports_foreign_and_manual_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = "Canonical policy must be sourced exactly once."
            (root / "policy.md").write_text(policy, encoding="utf-8")
            (root / "GROK.md").write_text(policy, encoding="utf-8")
            (root / "foreign.md").write_text(policy, encoding="utf-8")
            manifest = root / "context.json"
            manifest.write_text(json.dumps({"modules": [{"id": "policy", "path": "policy.md"}], "adapters": [{"client": "grok", "path": "GROK.md", "modules": ["policy"]}], "foreign_sources": ["foreign.md"]}), encoding="utf-8")
            status = context_status(manifest)
            check = install_context_adapters(manifest, check=True)
        self.assertFalse(status["ok"])
        self.assertFalse(check["ok"])
        self.assertEqual({row["source"] for row in status["overlaps"]}, {"adapter:grok", "foreign"})


class StableMachineIdTests(unittest.TestCase):
    """The registry key must not follow a network-dependent hostname."""

    def setUp(self) -> None:
        readiness._MACHINE_ID_CACHE.clear()
        self._saved = os.environ.pop("AGENT_BRIDGE_MACHINE_ID", None)

    def tearDown(self) -> None:
        readiness._MACHINE_ID_CACHE.clear()
        if self._saved is not None:
            os.environ["AGENT_BRIDGE_MACHINE_ID"] = self._saved

    def test_id_is_stable_across_hostname_changes(self) -> None:
        with mock.patch.object(readiness.socket, "gethostname", return_value="workstation.local"):
            first = readiness.machine_id()
        readiness._MACHINE_ID_CACHE.clear()
        with mock.patch.object(readiness.socket, "gethostname", return_value="workstation.corp"):
            second = readiness.machine_id()
        self.assertEqual(first, second)

    def test_explicit_override_wins(self) -> None:
        os.environ["AGENT_BRIDGE_MACHINE_ID"] = "ci-runner-7"
        self.assertEqual(readiness.machine_id(), "ci-runner-7")

    def test_id_hashes_platform_uuid_without_exposing_it(self) -> None:
        raw_uuid = "01234567-89AB-CDEF-0123-456789ABCDEF"
        expected_hash = hashlib.sha256(raw_uuid.encode("utf-8")).hexdigest()[:8]
        with mock.patch.object(readiness.getpass, "getuser", return_value="alice"):
            with mock.patch.object(readiness, "platform_uuid", return_value=raw_uuid):
                value = readiness.machine_id()
        self.assertEqual(value, f"alice_{expected_hash}")
        self.assertNotIn(raw_uuid, value)

    def test_falls_back_to_hostname_without_platform_uuid(self) -> None:
        with mock.patch.object(readiness, "platform_uuid", return_value=None):
            with mock.patch.object(readiness.socket, "gethostname", return_value="host.example"):
                self.assertIn("host", readiness.machine_id())

    def test_cli_and_readiness_agree(self) -> None:
        from agent_bridge.cli import _harness_machine_id

        self.assertEqual(_harness_machine_id(), readiness.machine_id())


if __name__ == "__main__":
    unittest.main()
