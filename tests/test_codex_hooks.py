"""Codex hook-trust auditing and bounded repair.

The failure these guard against is silent: Codex loads a hook whose positional
trust record no longer matches, reports it as present in every configuration
file, and then skips it.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_bridge import cli as bridge_cli
from agent_bridge import codex_hooks


def handler(
    key: str,
    command: str,
    *,
    trust: str = "trusted",
    enabled: bool = True,
    current_hash: str = "sha256:abc",
    builtin: bool = False,
) -> dict:
    return {
        "key": key,
        "event": "sessionStart",
        "matcher": "startup|resume",
        "command": command,
        "trust_status": trust,
        "enabled": enabled,
        "current_hash": current_hash,
        "builtin": builtin,
        "source_path": "/home/u/.codex/hooks.json",
        "owned": codex_hooks.is_owned(command),
        "runs": trust in {"trusted", "managed"} and enabled,
    }


CONFIG = """\
model = "gpt-5"

[hooks.state."/h/hooks.json:session_start:0:0"]
trusted_hash = "sha256:live"

[hooks.state."/h/hooks.json:session_start:0:4"]
trusted_hash = "sha256:orphan"

[projects."/h/code"]
trust_level = "trusted"
"""


class OwnershipTests(unittest.TestCase):
    def test_ownership_follows_source_roots_and_env_override(self) -> None:
        owned = str(Path("~/Code/agent-bridge").expanduser())
        self.assertTrue(codex_hooks.is_owned(f"'{owned}/bin/agent' code hook session-start"))
        self.assertFalse(codex_hooks.is_owned("/opt/vendor/plugin.json#hooks[0]"))
        self.assertFalse(codex_hooks.is_owned(""))
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_CODEX_OWNED_ROOTS": "/srv/mine"}):
            self.assertTrue(codex_hooks.is_owned("/srv/mine/hook.sh"))
            self.assertFalse(codex_hooks.is_owned(f"{owned}/bin/agent"))


class StaleStateTests(unittest.TestCase):
    def test_stale_keys_are_those_addressing_no_live_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(CONFIG, encoding="utf-8")
            handlers = [handler("/h/hooks.json:session_start:0:0", "/h/ok.sh")]
            stale = codex_hooks.stale_state_keys(handlers, config_path=config)
        self.assertEqual(stale, ["/h/hooks.json:session_start:0:4"])

    def test_prune_removes_only_stale_blocks_and_keeps_a_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(CONFIG, encoding="utf-8")
            handlers = [handler("/h/hooks.json:session_start:0:0", "/h/ok.sh")]

            reported = codex_hooks.prune_stale_state(handlers, config_path=config, apply_changes=False)
            self.assertEqual(reported["removed"], [])
            self.assertEqual(config.read_text(encoding="utf-8"), CONFIG)

            applied = codex_hooks.prune_stale_state(handlers, config_path=config, apply_changes=True)
            remaining = config.read_text(encoding="utf-8")
            backup = Path(applied["backup"])

            self.assertEqual(applied["removed"], ["/h/hooks.json:session_start:0:4"])
            self.assertNotIn("session_start:0:4", remaining)
            self.assertIn('[hooks.state."/h/hooks.json:session_start:0:0"]', remaining)
            self.assertIn('trusted_hash = "sha256:live"', remaining)
            self.assertIn('[projects."/h/code"]', remaining)
            self.assertIn('model = "gpt-5"', remaining)
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(encoding="utf-8"), CONFIG)

    def test_prune_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.toml"
            config.write_text(CONFIG, encoding="utf-8")
            handlers = [handler("/h/hooks.json:session_start:0:0", "/h/ok.sh")]
            codex_hooks.prune_stale_state(handlers, config_path=config, apply_changes=True)
            first = config.read_text(encoding="utf-8")
            second = codex_hooks.prune_stale_state(handlers, config_path=config, apply_changes=True)
        self.assertEqual(second["removed"], [])
        self.assertEqual(second["backup"], None)
        self.assertIn("session_start:0:0", first)


class RepairScopeTests(unittest.TestCase):
    def test_repair_trusts_only_owned_blocked_handlers(self) -> None:
        owned_root = str(Path("~/Code/skills-vault").expanduser())
        handlers = [
            handler("k:0:0", f"{owned_root}/scripts/start.sh", trust="modified", current_hash="sha256:one"),
            handler("k:0:1", f"{owned_root}/scripts/sync.py", trust="untrusted", current_hash="sha256:two"),
            handler("k:0:2", "/opt/vendor/thing.sh", trust="untrusted", current_hash="sha256:three"),
            handler("k:0:3", f"{owned_root}/scripts/fine.sh", trust="trusted", current_hash="sha256:four"),
        ]
        sent: list[dict] = []

        class FakeServer:
            def __init__(self, timeout: int = 20) -> None:
                self.timeout = timeout

            def handshake(self) -> None:
                return None

            def send(self, value: dict) -> None:
                sent.append(value)

            def wait(self, request_id: int) -> dict:
                return {"id": request_id, "result": {}}

            def close(self) -> None:
                return None

        with mock.patch.object(codex_hooks, "_AppServer", FakeServer):
            outcome = codex_hooks.repair_trust(handlers)

        edits = sent[0]["params"]["edits"]
        self.assertEqual(sent[0]["method"], "config/batchWrite")
        self.assertTrue(sent[0]["params"]["reloadUserConfig"])
        self.assertEqual(
            [edit["keyPath"] for edit in edits],
            ['hooks.state."k:0:0".trusted_hash', 'hooks.state."k:0:1".trusted_hash'],
        )
        self.assertEqual([edit["value"] for edit in edits], ["sha256:one", "sha256:two"])
        self.assertEqual([item["key"] for item in outcome["trusted"]], ["k:0:0", "k:0:1"])
        self.assertEqual([item["key"] for item in outcome["skipped"]], ["k:0:2"])

    def test_repair_sends_nothing_when_no_owned_hook_is_blocked(self) -> None:
        handlers = [handler("k:0:0", str(Path("~/Code/agent-bridge/bin/agent").expanduser()))]
        with mock.patch.object(codex_hooks, "_AppServer", side_effect=AssertionError("must not connect")):
            outcome = codex_hooks.repair_trust(handlers)
        self.assertEqual(outcome, {"trusted": [], "skipped": []})


class TrustAuditCliTests(unittest.TestCase):
    def test_missing_codex_degrades_to_unavailable_not_blocked(self) -> None:
        with mock.patch.object(
            bridge_cli, "codex_hook_audit", side_effect=codex_hooks.CodexHooksError("codex is not on PATH")
        ):
            report = bridge_cli._codex_trust_report()
        self.assertFalse(report["available"])
        self.assertEqual(report["blocked_owned"], [])
        self.assertIn("codex is not on PATH", bridge_cli._codex_trust_lines(report)[0])

    def test_audit_exits_non_zero_only_when_an_owned_hook_is_blocked(self) -> None:
        blocked = handler("k:0:1", str(Path("~/Code/agent-bridge/bin/agent").expanduser()), trust="modified")
        report = {
            "available": True,
            "total": 2,
            "running": 1,
            "blocked": [blocked],
            "blocked_owned": [blocked],
            "blocked_foreign": [],
            "handlers": [blocked],
            "stale_state_keys": ["k:0:9"],
        }
        with mock.patch.object(bridge_cli, "_codex_trust_report", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = bridge_cli.hooks_cmd(["audit", "--client", "codex"])
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", buffer.getvalue())
        self.assertIn("STALE trust record", buffer.getvalue())

        healthy = {**report, "blocked": [], "blocked_owned": [], "handlers": [], "stale_state_keys": []}
        with mock.patch.object(bridge_cli, "_codex_trust_report", return_value=healthy):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(bridge_cli.hooks_cmd(["audit", "--client", "codex"]), 0)

    def test_audit_reports_unavailable_registry_without_claiming_breakage(self) -> None:
        unavailable = {"available": False, "detail": "codex is not on PATH", "blocked_owned": [], "blocked_foreign": [], "stale_state_keys": []}
        with mock.patch.object(bridge_cli, "_codex_trust_report", return_value=unavailable):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = bridge_cli.hooks_cmd(["audit", "--client", "codex", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["available"])

    def test_repair_trust_report_only_mode_writes_nothing(self) -> None:
        blocked = handler("k:0:1", str(Path("~/Code/skills-vault/x.sh").expanduser()), trust="untrusted")
        report = {
            "available": True,
            "total": 1,
            "running": 0,
            "blocked": [blocked],
            "blocked_owned": [blocked],
            "blocked_foreign": [],
            "handlers": [blocked],
            "stale_state_keys": ["k:0:9"],
        }
        with mock.patch.object(bridge_cli, "_codex_trust_report", return_value=report):
            with mock.patch.object(bridge_cli, "repair_trust", side_effect=AssertionError("must not write")):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = bridge_cli.hooks_cmd(["repair-trust", "--client", "codex"])
        output = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("would trust", output)
        self.assertIn("would prune stale trust record", output)
        self.assertIn("pass --apply", output)

    def test_trust_commands_refuse_clients_without_a_hook_registry(self) -> None:
        for command in ("audit", "repair-trust"):
            with self.assertRaises(bridge_cli.BridgeError):
                bridge_cli.hooks_cmd([command, "--client", "claude"])


if __name__ == "__main__":
    unittest.main()
