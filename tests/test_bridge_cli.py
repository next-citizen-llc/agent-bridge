from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "bin" / "agent"


class BridgeCliTests(unittest.TestCase):
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
            }
            proc = subprocess.run(
                [str(AGENT), "code", "hook", "session-start", "--client", "codex"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            registry_file = shared_root / "Agent-Bridge" / "registry" / "test-machine.codex.json"
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
            self.assertIn("name: agent-bridge", skill.read_text(encoding="utf-8"))
            self.assertEqual(codex_link.resolve(), (shared_root / "Agent-Bridge").resolve())
            self.assertEqual(claude_link.resolve(), (shared_root / "Agent-Bridge").resolve())
            self.assertEqual(agents_link.resolve(), (shared_root / "Agent-Bridge").resolve())

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


if __name__ == "__main__":
    unittest.main()
