from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from agent_bridge import cli as bridge_cli
from agent_bridge.cli import is_auth_error


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "bin" / "agent"


class BridgeCliTests(unittest.TestCase):
    def test_auth_error_recognizes_logged_out_claude_cli(self) -> None:
        self.assertTrue(is_auth_error("Not logged in · Please run /login"))

    def test_code_readiness_gate_refuses_blocked_required_probe_and_traces_decision(self) -> None:
        report = {
            "generated_at": "2030-01-01T00:00:00Z",
            "overall": "blocked",
            "project_dir": str(bridge_cli.PROJECT_DIR),
            "checks": [{"name": "github_identity", "required": True, "status": "blocked"}],
        }
        with mock.patch.object(bridge_cli, "load_cached_preflight", return_value=report):
            with mock.patch.object(bridge_cli, "emit_event") as emit:
                with mock.patch.object(bridge_cli, "record_run_task"):
                    allowed = bridge_cli._dispatch_readiness_gate(
                        "codex",
                        mode="code",
                        command="bridge",
                        meta={"run_id": "run_test"},
                        no_preflight=False,
                        require_ready=False,
                        refresh=False,
                        timeout=1,
                    )
        self.assertFalse(allowed)
        self.assertEqual(emit.call_args.kwargs["data"]["decision"], "refuse")

    def test_review_readiness_gate_warns_but_operator_bypass_is_explicit(self) -> None:
        report = {"generated_at": "2030-01-01T00:00:00Z", "overall": "degraded", "project_dir": str(bridge_cli.PROJECT_DIR), "checks": []}
        with mock.patch.object(bridge_cli, "load_cached_preflight", return_value=report):
            with mock.patch.object(bridge_cli, "emit_event") as emit:
                with mock.patch.object(bridge_cli, "record_run_task"):
                    allowed = bridge_cli._dispatch_readiness_gate(
                        "grok",
                        mode="review",
                        command="bridge",
                        meta={"run_id": "run_review"},
                        no_preflight=False,
                        require_ready=False,
                        refresh=False,
                        timeout=1,
                    )
                    bypassed = bridge_cli._dispatch_readiness_gate(
                        "grok",
                        mode="code",
                        command="bridge",
                        meta={"run_id": "run_bypass"},
                        no_preflight=True,
                        require_ready=True,
                        refresh=False,
                        timeout=1,
                    )
        self.assertTrue(allowed)
        self.assertTrue(bypassed)
        self.assertEqual(emit.call_args.kwargs["data"]["decision"], "bypass")

    def test_require_ready_refuses_unknown_advisory_even_when_overall_is_ready(self) -> None:
        report = {
            "generated_at": "2030-01-01T00:00:00Z",
            "overall": "ready",
            "project_dir": str(bridge_cli.PROJECT_DIR),
            "checks": [
                {"name": "client_binary", "required": True, "status": "ready"},
                {"name": "mcp_health", "required": False, "status": "unknown"},
            ],
        }
        with mock.patch.object(bridge_cli, "load_cached_preflight", return_value=report):
            with mock.patch.object(bridge_cli, "emit_event") as emit:
                with mock.patch.object(bridge_cli, "record_run_task"):
                    allowed = bridge_cli._dispatch_readiness_gate(
                        "codex",
                        mode="review",
                        command="bridge",
                        meta={"run_id": "run_strict"},
                        no_preflight=False,
                        require_ready=True,
                        refresh=False,
                        timeout=1,
                    )
        self.assertFalse(allowed)
        self.assertFalse(emit.call_args.kwargs["data"]["strict_ready"])

    def test_readiness_gate_refreshes_cache_from_another_project(self) -> None:
        cached = {"generated_at": "2030-01-01T00:00:00Z", "overall": "ready", "project_dir": "/different/project", "checks": []}
        live = {"generated_at": "2030-01-01T00:00:01Z", "overall": "ready", "project_dir": str(bridge_cli.PROJECT_DIR), "checks": []}
        with mock.patch.object(bridge_cli, "load_cached_preflight", return_value=cached):
            with mock.patch.object(bridge_cli, "run_preflight", return_value=live) as run:
                with mock.patch.object(bridge_cli, "emit_event"):
                    with mock.patch.object(bridge_cli, "record_run_task"):
                        allowed = bridge_cli._dispatch_readiness_gate(
                            "codex",
                            mode="code",
                            command="bridge",
                            meta={"run_id": "run_project_scope"},
                            no_preflight=False,
                            require_ready=False,
                            refresh=False,
                            timeout=1,
                        )
        self.assertTrue(allowed)
        run.assert_called_once()

    def test_context_check_exits_nonzero_for_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = "Canonical context text must not be duplicated."
            (root / "policy.md").write_text(policy, encoding="utf-8")
            (root / "GROK.md").write_text(policy, encoding="utf-8")
            manifest = root / "context.json"
            manifest.write_text(
                json.dumps(
                    {
                        "modules": [{"id": "policy", "path": "policy.md"}],
                        "adapters": [{"client": "grok", "path": "GROK.md", "modules": ["policy"]}],
                    }
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [str(AGENT), "code", "context", "check", "--manifest", str(manifest)],
                cwd=str(ROOT),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("overlap:policy", proc.stdout)

    def _write_fake_claude(self, tmp: str) -> Path:
        fake = Path(tmp) / "fake_claude.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "log = Path(os.environ['FAKE_CLAUDE_LOG'])\n"
            "marker = Path(os.environ.get('FAKE_CLAUDE_AUTH_MARKER', log.with_suffix('.auth')))\n"
            "def log_line(text):\n"
            "    log.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with log.open('a', encoding='utf-8') as handle:\n"
            "        handle.write(text + '\\n')\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['auth', 'status']:\n"
            "    print(json.dumps({'loggedIn': True, 'email': 'user@example.test'}))\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['auth', 'logout']:\n"
            "    log_line('logout')\n"
            "    raise SystemExit(0)\n"
            "if args[:2] == ['auth', 'login']:\n"
            "    log_line('login ' + ' '.join(args[2:]))\n"
            "    marker.write_text('ok', encoding='utf-8')\n"
            "    print('Login successful.')\n"
            "    raise SystemExit(0)\n"
            "if '-p' in args:\n"
            "    prompt = args[args.index('-p') + 1]\n"
            "    budget = '0'\n"
            "    if '--max-budget-usd' in args:\n"
            "        budget = args[args.index('--max-budget-usd') + 1]\n"
            "    log_line('budget ' + budget)\n"
            "    if os.environ.get('FAKE_CLAUDE_AUTH_FAIL') == '1' and not marker.exists():\n"
            "        print('Failed to authenticate. API Error: 401 Invalid authentication credentials')\n"
            "        raise SystemExit(1)\n"
            "    if float(budget) < float(os.environ.get('FAKE_CLAUDE_MIN_BUDGET', '0.5')):\n"
            "        print(f'Error: Exceeded USD budget ({budget})')\n"
            "        raise SystemExit(1)\n"
            "    if 'CLAUDE_DIRECT_OK' in prompt:\n"
            "        print('CLAUDE_DIRECT_OK')\n"
            "    elif 'BRIDGE_REPAIR_OK' in prompt:\n"
            "        print('BRIDGE_REPAIR_OK')\n"
            "    elif 'BRIDGE_LIVE_OK' in prompt:\n"
            "        print('BRIDGE_LIVE_OK')\n"
            "    else:\n"
            "        print('FAKE_CLAUDE_OK')\n"
            "    raise SystemExit(0)\n"
            "print('unexpected fake claude args: ' + ' '.join(args))\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake

    def test_list_agents(self) -> None:
        proc = subprocess.run(
            [str(AGENT), "code", "bridge", "--list"],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("claude", proc.stdout)
        self.assertIn("codex", proc.stdout)

    def test_dry_run_discovers_current_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "sample"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "claude",
                    "--mode",
                    "review",
                    "--prompt",
                    "report scope",
                    "--dry-run",
                ],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            expected_project = repo.resolve()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Project: {expected_project}", proc.stdout)
        self.assertIn("--permission-mode auto", proc.stdout)
        self.assertIn("--allowedTools Read,Grep,Glob", proc.stdout)

    def test_loop_dry_run_emits_ordered_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "loop",
                    "--builder",
                    "claude",
                    "--critic",
                    "claude",
                    "--verifier",
                    "claude",
                    "--max-turns",
                    "1",
                    "--spawn-policy",
                    "full",
                    "--prompt",
                    "loop smoke",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            events_path = Path(tmp) / "state" / "events.jsonl"
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.count("[dry-run] claude:"), 3)
            self.assertIn("run_id:", proc.stdout)
            rows = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 9)
        self.assertIn('"type": "run.created"', rows[0])
        self.assertIn('"type": "dispatch.policy_evaluated"', rows[1])
        self.assertIn('"role": "builder"', rows[2])
        self.assertIn('"role": "critic"', rows[4])
        self.assertIn('"role": "verifier"', rows[6])
        self.assertIn('"type": "run.completed"', rows[-1])

    def test_loop_auto_uses_one_adversarial_agent_for_vague_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "loop",
                    "--builder",
                    "claude",
                    "--critic",
                    "claude",
                    "--verifier",
                    "claude",
                    "--max-turns",
                    "3",
                    "--prompt",
                    "quick check this",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            rows = [
                json.loads(line)
                for line in (Path(tmp) / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("[dry-run] claude:"), 1)
        self.assertIn("dispatch_decision: adversarial_only", proc.stdout)
        dispatched = [row for row in rows if row["type"] == "agent.dispatched"]
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["role"], "adversarial")
        self.assertEqual(dispatched[0]["data"]["target"], "claude")

    def test_loop_auto_allows_full_loop_for_scoped_implementation(self) -> None:
        prompt = (
            "Implement a schema and trace controller update in agent_bridge/cli.py "
            "and tests/test_bridge_cli.py with backwards compatible workflow coverage "
            "and adversarial validation."
        )
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "loop",
                    "--builder",
                    "claude",
                    "--critic",
                    "claude",
                    "--verifier",
                    "claude",
                    "--prompt",
                    prompt,
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            rows = [
                json.loads(line)
                for line in (Path(tmp) / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("[dry-run] claude:"), 3)
        self.assertIn("dispatch_decision: full_loop", proc.stdout)
        self.assertEqual([row["role"] for row in rows if row["type"] == "agent.dispatched"], ["builder", "critic", "verifier"])

    def test_codex_dry_run_uses_current_exec_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "codex",
                    "--mode",
                    "review",
                    "--prompt",
                    "review scope",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("codex exec", proc.stdout)
        self.assertIn("-s read-only", proc.stdout)
        self.assertNotIn("-a never", proc.stdout)

    def test_bridge_dry_run_converts_heic_prompt_paths_for_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "sample"
            repo.mkdir()
            photo = repo / "Vacation Photo.HEIC"
            photo.write_bytes(b"fake heic")
            converter = Path(tmp) / "convert_heic.py"
            converter.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "Path(sys.argv[2]).write_bytes(b'fake png')\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state"),
                "AGENT_BRIDGE_HEIC_CONVERTER": f"{sys.executable} {converter}",
            }
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "claude",
                    "--mode",
                    "review",
                    "--prompt",
                    f'Please inspect "{photo}"',
                    "--dry-run",
                ],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            converted = list((Path(tmp) / "state" / "media").rglob("*.png"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(converted), 1)
        self.assertIn("[AGENT BRIDGE MEDIA]", proc.stdout)
        self.assertIn(str(photo), proc.stdout)
        self.assertIn(str(converted[0]), proc.stdout)
        self.assertIn(f"--add-dir {converted[0].parent}", proc.stdout)

    def test_bridge_heic_conversion_failure_does_not_block_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "sample"
            repo.mkdir()
            photo = repo / "broken.heic"
            photo.write_bytes(b"fake heic")
            env = {
                **os.environ,
                "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state"),
                "AGENT_BRIDGE_HEIC_CONVERTER": str(Path(tmp) / "missing-converter"),
            }
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "claude",
                    "--mode",
                    "review",
                    "--prompt",
                    f"Please inspect {photo}",
                    "--dry-run",
                ],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Could not convert these HEIC/HEIF inputs", proc.stdout)
        self.assertIn("missing-converter", proc.stdout)

    def test_bridge_retries_and_records_budget_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._write_fake_claude(tmp)
            state = Path(tmp) / "state"
            log = Path(tmp) / "fake.log"
            env = {
                **os.environ,
                "AGENT_BRIDGE_STATE_DIR": str(state),
                "CLAUDE_BIN": str(fake),
                "FAKE_CLAUDE_LOG": str(log),
                "FAKE_CLAUDE_MIN_BUDGET": "0.5",
            }
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "claude",
                    "--mode",
                    "review",
                    "--budget-usd",
                    "0.05",
                    "--prompt",
                    "Reply exactly: BRIDGE_LIVE_OK",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            state_payload = json.loads((state / "connections.json").read_text(encoding="utf-8"))
            log_lines = [line.strip() for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BRIDGE_LIVE_OK", proc.stdout)
        self.assertIn("budget 0.05 was too low; retrying with 0.1", proc.stderr)
        self.assertIn("budget 0.2 was too low; retrying with 0.5", proc.stderr)
        self.assertEqual(state_payload["agents"]["claude"]["calibrated_budget_usd"], "0.5")
        self.assertEqual(log_lines, ["budget 0.05", "budget 0.1", "budget 0.2", "budget 0.5"])

    def test_repair_refreshes_claude_auth_and_checks_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = self._write_fake_claude(tmp)
            state = Path(tmp) / "state"
            log = Path(tmp) / "fake.log"
            marker = Path(tmp) / "auth.marker"
            env = {
                **os.environ,
                "AGENT_BRIDGE_STATE_DIR": str(state),
                "CLAUDE_BIN": str(fake),
                "FAKE_CLAUDE_LOG": str(log),
                "FAKE_CLAUDE_AUTH_MARKER": str(marker),
                "FAKE_CLAUDE_AUTH_FAIL": "1",
                "FAKE_CLAUDE_MIN_BUDGET": "0.5",
                "AGENT_BRIDGE_CLAUDE_EMAIL": "user@example.test",
            }
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "repair",
                    "--to",
                    "claude",
                    "--budget-usd",
                    "0.5",
                    "--max-auto-budget-usd",
                    "0.5",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            state_payload = json.loads((state / "connections.json").read_text(encoding="utf-8"))
            marker_exists = marker.exists()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(marker_exists)
        self.assertIn("claude direct probe failed auth; refreshing Claude login", proc.stdout)
        self.assertIn("claude direct probe: ok at budget 0.5", proc.stdout)
        self.assertIn("BRIDGE_REPAIR_OK", proc.stdout)
        self.assertEqual(state_payload["agents"]["claude"]["last_status"], "ok")

    def test_bridge_child_role_uses_target_not_forwarded_caller_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "claude",
                    "--mode",
                    "review",
                    "--prompt",
                    "role smoke",
                    "--run-id",
                    "run-role",
                    "--loop-id",
                    "loop-role",
                    "--turn-id",
                    "caller-turn",
                    "--parent-id",
                    "parent-turn",
                    "--role",
                    "caller",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            rows = [
                json.loads(line)
                for line in (Path(tmp) / "state" / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dispatched = next(row for row in rows if row["type"] == "agent.dispatched")
        self.assertEqual(dispatched["role"], "claude")
        self.assertEqual(dispatched["parent_id"], "parent-turn")
        self.assertNotEqual(dispatched["turn_id"], "caller-turn")

    def test_bridge_writes_correlation_to_transcript_header_and_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "agents.json"
            state = Path(tmp) / "state"
            config.write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "id": "helper",
                                "label": "Helper",
                                "adapter": "argv",
                                "command": "python3",
                                "args": ["-c", "print('agent output')"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(state)}
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--config",
                    str(config),
                    "--from",
                    "human",
                    "--to",
                    "helper",
                    "--mode",
                    "review",
                    "--prompt",
                    "transcript smoke",
                    "--run-id",
                    "run.transcript",
                    "--loop-id",
                    "loop.transcript",
                    "--parent-id",
                    "parent-transcript",
                    "--attempt",
                    "3",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            transcripts = list((state / "transcripts").glob("*.txt"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(transcripts), 1)
            transcript_name = transcripts[0].name
            text = transcripts[0].read_text(encoding="utf-8")
        self.assertIn("run_transcript_", transcript_name)
        self.assertIn("_helper_", transcript_name)
        self.assertIn("correlation: ", text)
        self.assertIn("run_id=run.transcript", text)
        self.assertIn("loop_id=loop.transcript", text)
        self.assertIn("parent_id=parent-transcript", text)
        self.assertIn("attempt=3", text)
        self.assertIn("role=helper", text)
        self.assertRegex(text, r"turn_id=turn_helper_")

    def test_session_start_hook_outputs_context_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared_root = Path(tmp) / "SharedAgentSkills"
            (shared_root / "Agent-Bridge").mkdir(parents=True)
            env = {
                **os.environ,
                "AGENT_BRIDGE_SHARED_SKILLS_ROOT": str(shared_root),
                "AGENT_BRIDGE_MACHINE_ID": "test-machine",
                "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state"),
                "AGENT_BRIDGE_DISABLE_AUTO_UPDATE": "1",
            }
            proc = subprocess.run(
                [str(AGENT), "code", "hook", "session-start", "--client", "codex", "--surface", "cli"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            registry_file = shared_root / "Agent-Bridge" / "registry" / "test-machine.codex.cli.json"
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            output = payload["hookSpecificOutput"]
            self.assertEqual(output["hookEventName"], "SessionStart")
            self.assertIn("Agent Bridge session bootstrap", output["additionalContext"])
            self.assertIn("never spawns agents", output["additionalContext"])
            self.assertIn("agent code harness status", output["additionalContext"])
            self.assertIn(str(registry_file), output["additionalContext"])
            self.assertIn(str(ROOT / "agent_bridge" / "mailbox_mcp.py"), output["additionalContext"])
            self.assertTrue(registry_file.exists())
            registration = json.loads(registry_file.read_text(encoding="utf-8"))
            self.assertEqual(registration["surface"], "cli")
            self.assertFalse(registration["registration_proves_auth"])
            self.assertEqual(registration["bridge_revision"], subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
            ).strip())
            self.assertIn("deployed_revision", registration)
            self.assertIn("update_status", registration)
            self.assertNotIn("platform_uuid", registration)

    def test_harness_register_and_status_use_shared_agent_skills_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared_root = Path(tmp) / "SharedAgentSkills"
            env = {
                **os.environ,
                "AGENT_BRIDGE_MACHINE_ID": "test-machine",
                "AGENT_BRIDGE_SHARED_SKILLS_ROOT": str(shared_root),
            }
            register = subprocess.run(
                [str(AGENT), "code", "harness", "register", "--client", "codex"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            status = subprocess.run(
                [str(AGENT), "code", "harness", "status", "--json"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(register.returncode, 0, register.stderr)
            self.assertIn("test-machine.codex.json", register.stdout)
            self.assertEqual(status.returncode, 0, status.stderr)
            payload = json.loads(status.stdout)
            self.assertEqual(len(payload["harnesses"]), 1)
            self.assertEqual(payload["harnesses"][0]["client"], "codex")
            self.assertTrue(payload["harnesses"][0]["fresh"])

    def test_harness_status_prunes_only_expired_registry_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared_root = Path(tmp) / "SharedAgentSkills"
            registry = shared_root / "Agent-Bridge" / "registry"
            registry.mkdir(parents=True)
            old = registry / "old.codex.gui.json"
            fresh = registry / "fresh.codex.gui.json"
            old.write_text(json.dumps({"updated_at": "2000-01-01T00:00:00Z", "status": "active"}), encoding="utf-8")
            fresh.write_text(json.dumps({"updated_at": bridge_cli.iso_now(), "status": "active"}), encoding="utf-8")

            payload = bridge_cli.load_harness_registry(str(shared_root))

            self.assertEqual(payload["pruned"], [old.name])
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertEqual([Path(row["registry_file"]).name for row in payload["harnesses"]], [fresh.name])

    def test_harness_status_no_prune_retains_expired_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared_root = Path(tmp) / "SharedAgentSkills"
            registry = shared_root / "Agent-Bridge" / "registry"
            registry.mkdir(parents=True)
            old = registry / "old.codex.gui.json"
            old.write_text(json.dumps({"updated_at": "2000-01-01T00:00:00Z", "status": "active"}), encoding="utf-8")

            payload = bridge_cli.load_harness_registry(str(shared_root), prune=False)

            self.assertEqual(payload["pruned"], [])
            self.assertTrue(old.exists())
            self.assertEqual(len(payload["harnesses"]), 1)

    def test_session_start_reexecutes_once_after_bridge_update(self) -> None:
        update = {
            "status": "updated",
            "local_revision": "a" * 40,
            "deployed_revision": "a" * 40,
            "reexec_required": True,
        }
        with mock.patch.object(bridge_cli, "update_bridge", return_value=update):
            with mock.patch.object(bridge_cli.os, "execve", side_effect=RuntimeError("reexec")) as execute:
                with self.assertRaisesRegex(RuntimeError, "reexec"):
                    bridge_cli.hook_session_start(["--client", "codex", "--surface", "cli"])
        command = execute.call_args.args[1]
        environment = execute.call_args.args[2]
        self.assertIn("--skip-update", command)
        self.assertEqual(environment["AGENT_BRIDGE_UPDATE_REEXEC"], "1")

    def test_harness_install_skill_writes_skill_and_local_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shared_root = Path(tmp) / "SharedAgentSkills"
            env = {**os.environ, "HOME": str(home), "AGENT_BRIDGE_SHARED_SKILLS_ROOT": str(shared_root)}
            proc = subprocess.run(
                [str(AGENT), "code", "harness", "install-skill", "--json"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            skill = shared_root / "Agent-Bridge" / "SKILL.md"
            codex_link = home / ".codex" / "skills" / "agent-bridge"
            claude_link = home / ".claude" / "skills" / "agent-bridge"
            agents_link = home / ".agents" / "skills" / "agent-bridge"
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["skill_path"], str(skill))
            self.assertTrue(skill.exists())
            skill_text = skill.read_text(encoding="utf-8")
            self.assertIn("name: agent-bridge", skill_text)
            self.assertIn("agent code preflight configure", skill_text)
            self.assertIn("agent code context check", skill_text)
            self.assertIn("agent code update <status|check|apply>", skill_text)
            self.assertIn("raw platform identifier is never written", skill_text)
            self.assertIn("agent code harness status --no-prune", skill_text)
            self.assertEqual(codex_link.resolve(), (shared_root / "Agent-Bridge").resolve())
            self.assertEqual(claude_link.resolve(), (shared_root / "Agent-Bridge").resolve())
            self.assertEqual(agents_link.resolve(), (shared_root / "Agent-Bridge").resolve())
            self.assertEqual((home / ".grok" / "skills" / "agent-bridge").resolve(), (shared_root / "Agent-Bridge").resolve())

    def test_install_skill_retargets_a_stale_grok_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shared_root = Path(tmp) / "SharedAgentSkills"
            stale = Path(tmp) / "old-skill"
            stale.mkdir()
            grok_link = home / ".grok" / "skills" / "agent-bridge"
            grok_link.parent.mkdir(parents=True)
            grok_link.symlink_to(stale, target_is_directory=True)
            env = {**os.environ, "HOME": str(home), "AGENT_BRIDGE_SHARED_SKILLS_ROOT": str(shared_root)}
            proc = subprocess.run(
                [str(AGENT), "code", "harness", "install-skill", "--link-client", "grok", "--json"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            payload = json.loads(proc.stdout)
            resolved_grok_link = grok_link.resolve()
            resolved_shared_skill = (shared_root / "Agent-Bridge").resolve()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(payload["links"][0]["status"], "retargeted")
        self.assertEqual(resolved_grok_link, resolved_shared_skill)

    def test_hooks_install_is_idempotent_for_codex_and_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "HOME": tmp, "AGENT_BRIDGE_HOOK_AGENT": "/tmp/agent"}
            codex_dir = Path(tmp) / ".codex"
            claude_dir = Path(tmp) / ".claude"
            codex_dir.mkdir()
            claude_dir.mkdir()
            (codex_dir / "hooks.json").write_text('{"hooks":{"SessionStart":[]}}\n', encoding="utf-8")
            (claude_dir / "settings.json").write_text('{"model":"opus","hooks":{}}\n', encoding="utf-8")

            for _ in range(2):
                proc = subprocess.run(
                    [str(AGENT), "code", "hooks", "install", "--client", "both"],
                    cwd=str(ROOT),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

            codex = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
            claude = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))

        codex_hooks = codex["hooks"]["SessionStart"][0]["hooks"]
        claude_hooks = claude["hooks"]["SessionStart"][0]["hooks"]
        self.assertEqual(
            [hook["command"] for hook in codex_hooks].count("'/tmp/agent' code hook session-start --client codex"),
            1,
        )
        self.assertEqual(
            [hook["command"] for hook in claude_hooks].count("'/tmp/agent' code hook session-start --client claude"),
            1,
        )
        self.assertEqual(claude["model"], "opus")

    def test_hooks_uninstall_removes_only_agent_bridge_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "HOME": tmp, "AGENT_BRIDGE_HOOK_AGENT": "/tmp/agent"}
            codex_dir = Path(tmp) / ".codex"
            claude_dir = Path(tmp) / ".claude"
            codex_dir.mkdir()
            claude_dir.mkdir()
            unrelated = {"type": "command", "command": "/tmp/other-hook"}
            (codex_dir / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {"matcher": "startup|resume", "hooks": [unrelated]},
                            ]
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (claude_dir / "settings.json").write_text('{"model":"opus","hooks":{}}\n', encoding="utf-8")

            install = subprocess.run(
                [str(AGENT), "code", "hooks", "install", "--client", "both"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            uninstall = subprocess.run(
                [str(AGENT), "code", "hooks", "uninstall", "--client", "both", "--json"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            repeat = subprocess.run(
                [str(AGENT), "code", "hooks", "uninstall", "--client", "both"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            codex = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
            claude = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))

        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(
            codex["hooks"]["SessionStart"][0]["hooks"],
            [unrelated],
        )
        self.assertEqual(claude["hooks"]["SessionStart"], [])
        self.assertEqual(claude["model"], "opus")
        statuses = {row["status"] for row in json.loads(uninstall.stdout)["hooks"]}
        self.assertEqual(statuses, {"removed"})
        self.assertNotIn("agent-bridge", repeat.stderr)

    def test_hooks_install_uses_windows_cmd_wrapper_for_cmd_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "HOME": tmp,
                "AGENT_BRIDGE_HOOK_AGENT": r"C:\Users\me\.local\bin\agent.cmd",
            }
            codex_dir = Path(tmp) / ".codex"
            codex_dir.mkdir()
            proc = subprocess.run(
                [str(AGENT), "code", "hooks", "install", "--client", "codex"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            codex = json.loads((codex_dir / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        hook = codex["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(
            hook["command"],
            r'cmd /d /c ""C:\Users\me\.local\bin\agent.cmd" code hook session-start --client codex"',
        )

    def test_hooks_install_supports_grok_directory_and_surface_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "HOME": tmp, "AGENT_BRIDGE_HOOK_AGENT": "/tmp/agent"}
            install = subprocess.run(
                [str(AGENT), "code", "hooks", "install", "--client", "grok", "--json"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            status = subprocess.run(
                [str(AGENT), "code", "hooks", "status", "--client", "grok", "--json"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            path = Path(tmp) / ".grok" / "hooks" / "agent-bridge.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            wrapper_text = (Path(tmp) / ".local" / "bin" / "grok-gui-bridge").read_text(encoding="utf-8")
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        command = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(command, "'/tmp/agent' code hook session-start --client grok")
        rows = json.loads(status.stdout)["hooks"]
        self.assertEqual({row["surface"] for row in rows}, {"cli", "gui"})
        self.assertEqual(next(row for row in rows if row["surface"] == "cli")["status"], "installed")
        self.assertEqual(next(row for row in rows if row["surface"] == "gui")["status"], "installed")
        self.assertIn("https://grok.com/", wrapper_text)
        if sys.platform == "darwin":
            self.assertIn("Microsoft Edge", wrapper_text)
        else:
            self.assertRegex(wrapper_text, r"exec .*(microsoft-edge|xdg-open)")

    def test_hooks_uninstall_preserves_modified_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "HOME": tmp, "AGENT_BRIDGE_HOOK_AGENT": "/tmp/agent"}
            install = subprocess.run(
                [str(AGENT), "code", "hooks", "install", "--client", "grok"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            wrapper = Path(tmp) / ".local" / "bin" / "grok-gui-bridge"
            wrapper.write_text(wrapper.read_text(encoding="utf-8") + "# user edit\n", encoding="utf-8")
            uninstall = subprocess.run(
                [str(AGENT), "code", "hooks", "uninstall", "--client", "grok", "--json"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            native = Path(tmp) / ".grok" / "hooks" / "agent-bridge.json"
            config = json.loads(native.read_text(encoding="utf-8"))
            wrapper_exists = wrapper.exists()
            wrapper_contents = wrapper.read_text(encoding="utf-8")

        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertEqual(uninstall.returncode, 1, uninstall.stderr)
        rows = json.loads(uninstall.stdout)["hooks"]
        self.assertEqual(next(row for row in rows if row["surface"] == "cli")["status"], "removed")
        self.assertEqual(
            next(row for row in rows if row["surface"] == "gui")["status"],
            "modified-preserved",
        )
        self.assertTrue(wrapper_exists)
        self.assertTrue(wrapper_contents.endswith("# user edit\n"))
        self.assertEqual(config["hooks"]["SessionStart"], [])

    def test_code_dispatch_runs_work_preflight_and_honors_configured_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1 $2\" = \"login status\" ]; then echo 'Logged in'; exit 0; fi\n"
                "if [ \"$1 $2 $3\" = \"mcp list --json\" ]; then echo '[]'; exit 0; fi\n"
                "echo BRIDGE_PREFLIGHT_OK\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            fake_gh = root / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                "if [ \"$1 $2\" = \"auth status\" ]; then exit 0; fi\n"
                "if [ \"$1 $2\" = \"api user\" ]; then echo test-login; exit 0; fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}:/usr/bin:/bin",
                "CODEX_BIN": str(fake_codex),
                "AGENT_BRIDGE_STATE_DIR": str(root / "state"),
                "AGENT_BRIDGE_EXPECTED_GITHUB_LOGIN": "test-login",
                "AGENT_BRIDGE_SHARED_SKILLS_ROOT": str(root / "skills"),
                "AGENT_BRIDGE_SHARED_DATA_ROOT": str(root / "data"),
                "AGENT_BRIDGE_SHARED_CONVERSATIONS_ROOT": str(root / "conversations"),
            }
            for name in ("skills", "data", "conversations"):
                (root / name).mkdir()
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "codex",
                    "--mode",
                    "code",
                    "--prompt",
                    "bounded smoke test",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            cached = list((root / "state" / "readiness").glob("*.codex.bridge.work.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BRIDGE_PREFLIGHT_OK", proc.stdout)
        self.assertEqual(len(cached), 1)

    def test_code_dispatch_blocks_logged_out_zero_exit_and_override_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "dispatched"
            fake_codex = root / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1 $2\" = \"login status\" ]; then echo 'Not logged in · Please run /login'; exit 0; fi\n"
                "if [ \"$1 $2 $3\" = \"mcp list --json\" ]; then echo '[]'; exit 0; fi\n"
                f"touch {marker}\n"
                "echo OVERRIDE_DISPATCH_OK\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            fake_gh = root / "gh"
            fake_gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_gh.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(root),
                "PATH": f"{root}:/usr/bin:/bin",
                "CODEX_BIN": str(fake_codex),
                "AGENT_BRIDGE_STATE_DIR": str(root / "state"),
                "AGENT_BRIDGE_READINESS_CONFIG": str(root / "readiness.json"),
                "AGENT_BRIDGE_SHARED_SKILLS_ROOT": str(root / "skills"),
                "AGENT_BRIDGE_SHARED_DATA_ROOT": str(root / "data"),
                "AGENT_BRIDGE_SHARED_CONVERSATIONS_ROOT": str(root / "conversations"),
            }
            for name in ("skills", "data", "conversations"):
                (root / name).mkdir()
            base = [
                str(AGENT),
                "code",
                "bridge",
                "--from",
                "human",
                "--to",
                "codex",
                "--mode",
                "code",
                "--prompt",
                "bounded smoke test",
            ]
            blocked = subprocess.run(
                base,
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            dispatched_before_override = marker.exists()
            override = subprocess.run(
                [*base, "--no-preflight"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(blocked.returncode, 4, blocked.stderr)
        self.assertFalse(dispatched_before_override)
        self.assertEqual(override.returncode, 0, override.stderr)
        self.assertIn("OVERRIDE_DISPATCH_OK", override.stdout)


if __name__ == "__main__":
    unittest.main()
