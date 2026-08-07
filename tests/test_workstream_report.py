from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from agent_bridge.workstream_report import (
    ISSUE_PREVIEW_LIMIT,
    build_report_data,
    load_project_registry,
    load_workstream_catalog,
    normalise_machines,
    render_report,
    validate_workstreams,
    workspace_evidence,
)


WORKSTREAM_FIXTURES = [
    {
        "id": "sample-build",
        "title": "Sample build",
        "summary": "A public fixture workstream.",
        "objectives": ["Produce a verified result."],
        "projectSlugs": ["sample"],
        "repos": ["sample-repo"],
        "evidence": "high",
        "tags": ["fixture"],
    },
    {
        "id": "sample-research",
        "title": "Sample research",
        "summary": "A second public fixture workstream.",
        "objectives": ["Keep claims sourced."],
        "projectSlugs": ["research"],
        "repos": [],
        "evidence": "medium",
        "tags": ["fixture", "research"],
    },
]


CATALOG_FIXTURE = {
    "schemaVersion": "workstream-catalog/v1",
    "title": "Fixture control center",
    "githubOwner": "example",
    "inventory": {
        "capturedAt": "2026-08-07T00:00:00Z",
        "portableProjects": 2,
        "portableHistoricalCheckpoints": 3,
        "macCodexThreads": 4,
        "macClaudeSessions": 5,
        "bridgeDispatches": {"codex": 6},
        "limitations": [],
    },
    "workstreams": WORKSTREAM_FIXTURES,
    "method": {
        "principles": ["Work identity is stable."],
        "sources": [{"title": "Example", "url": "https://example.com"}],
    },
}


class WorkstreamReportTests(unittest.TestCase):
    def test_catalog_workstreams_have_unique_complete_ids(self) -> None:
        validate_workstreams(WORKSTREAM_FIXTURES)
        self.assertEqual(len(WORKSTREAM_FIXTURES), len({row["id"] for row in WORKSTREAM_FIXTURES}))

    def test_load_catalog_validates_schema_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(CATALOG_FIXTURE), encoding="utf-8")
            loaded = load_workstream_catalog(path)
            self.assertEqual("example", loaded["githubOwner"])
            self.assertEqual(2, len(loaded["workstreams"]))

            malformed = json.loads(json.dumps(CATALOG_FIXTURE))
            malformed["workstreams"][0]["objectives"] = "not-a-list"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "objectives must be a list"):
                load_workstream_catalog(path)

    def test_documented_example_catalog_loads(self) -> None:
        root = Path(__file__).resolve().parents[1]
        loaded = load_workstream_catalog(root / "docs" / "examples" / "workstream-catalog.example.json")
        self.assertEqual("workstream-catalog/v1", loaded["schemaVersion"])
        self.assertEqual(["example-runtime#12"], loaded["workstreams"][0]["issueRefs"])

    def test_normalise_machines_merges_rows_and_capabilities(self) -> None:
        registry = {
            "harnesses": [
                {
                    "machine_id": "machine-a",
                    "hostname": "MacBook-Pro",
                    "platform": "macOS-15",
                    "updated_at": "2026-08-07T10:00:00Z",
                    "fresh": True,
                    "client": "registry-only-client",
                    "surface": "cli",
                    "capabilities": [
                        {"id": "codex", "label": "Codex", "command_found": True, "modes": ["review", "code"]},
                        {"id": "agy", "label": "Anti-Gravity", "command_found": False, "modes": ["review"]},
                    ],
                },
                {
                    "machine_id": "machine-a",
                    "hostname": "MacBook-Pro",
                    "platform": "macOS-15",
                    "updated_at": "2026-08-07T11:00:00Z",
                    "fresh": True,
                    "client": "claude",
                    "surface": "gui",
                    "capabilities": [
                        {"id": "claude", "label": "Claude Code", "command_found": True, "modes": ["review", "code"]},
                    ],
                },
            ]
        }
        rows = normalise_machines(registry, current_machine="machine-a")
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["current"])
        self.assertEqual("macos", rows[0]["os"])
        self.assertEqual(["claude", "codex"], [row["id"] for row in rows[0]["agents"]])
        self.assertNotIn("registry-only-client", [row["id"] for row in rows[0]["agents"]])
        self.assertEqual(["cli", "gui"], rows[0]["surfaces"])

    def test_explicit_references_are_resolved_separately_from_repository_issues(self) -> None:
        catalog = json.loads(json.dumps(CATALOG_FIXTURE))
        catalog["workstreams"][0]["issueRefs"] = [
            "sample-repo#2",
            "missing-repo#99",
            "not-a-reference",
            "other-owner/sample-repo#2",
        ]
        catalog["workstreams"][0]["epicRefs"] = [
            {"repo": "sample-repo", "number": 1},
            "https://github.com/example/sample-repo/issues/1",
            "sample-repo#2",
        ]
        github_data = {
            "sample-repo": {
                "issues": [
                    {
                        "repo": "sample-repo", "number": 1, "title": "Epic", "state": "open", "updatedAt": "2026-08-07T00:00:00Z", "type": "Epic",
                        "isEpic": True, "labels": [], "milestone": None,
                    },
                    {
                        "repo": "sample-repo", "number": 2, "title": "[Epic] Scoped issue", "state": "open", "updatedAt": "2026-08-07T01:00:00Z", "type": "Task",
                        "isEpic": False, "labels": [], "milestone": None,
                    },
                ],
                "error": "",
            }
        }
        data = build_report_data(
            catalog=catalog,
            registry={"harnesses": []},
            current_machine="local",
            project_registry={},
            conversations_root=None,
            github_owner="example",
            github_data=github_data,
            generated_at="2026-08-07T00:00:00Z",
        )
        stream = data["workstreams"][0]
        self.assertEqual([1, 2], sorted(issue["number"] for issue in stream["repositoryIssues"]))
        self.assertEqual([2], [issue["number"] for issue in stream["relatedIssues"]])
        self.assertEqual([1], [issue["number"] for issue in stream["relatedEpics"]])
        self.assertEqual(4, len(stream["unresolvedRefs"]))
        self.assertIn("resolved issue is not an Epic", {row["reason"] for row in stream["unresolvedRefs"]})
        self.assertIn(
            "owner must match catalog GitHub owner example", {row["reason"] for row in stream["unresolvedRefs"]}
        )
        self.assertEqual(stream["repositoryIssues"], stream["issues"])

    def test_project_registry_and_workspace_evidence_accept_mixed_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "projects.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "slug": "sample",
                                "workspace_mac": "/Users/example/Code/sample",
                                "latest_windows": "C:\\Users\\example\\checkpoint.md",
                                "workspaces": {
                                    "os": "windows",
                                    "machine": "DESKTOP",
                                    "path": "C:\\Users\\example\\Code\\sample",
                                    "harness": "codex",
                                    "last_seen_at": "2026-08-07T00:00:00Z",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            conversations = root / "conversations"
            (conversations / "projects" / "sample").mkdir(parents=True)
            (conversations / "projects" / "sample" / "latest.md").write_text("checkpoint", encoding="utf-8")
            projects = load_project_registry(registry_path)
            workspaces, checkpoints = workspace_evidence(
                ["sample"], projects, conversations_root=conversations, current_machine="machine-a"
            )
            self.assertEqual({"macos", "windows"}, {row["os"] for row in workspaces})
            self.assertEqual({"sample"}, {row["projectSlug"] for row in workspaces})
            self.assertIn("portable", {row["os"] for row in checkpoints})
            self.assertIn("windows", {row["os"] for row in checkpoints})
            self.assertIn("machine-a", {row.get("machine") for row in checkpoints})

    def test_build_and_render_report_embeds_safe_self_contained_data(self) -> None:
        fake_github = {
            "sample-repo": {
                "repo": "sample-repo",
                "error": "",
                "issues": [
                    {
                        "repo": "sample-repo",
                        "number": 1,
                        "title": "Do not close </script><script>alert(1)</script>",
                        "url": "https://github.com/example/sample-repo/issues/1",
                        "state": "open",
                        "updatedAt": "2026-08-07T00:00:00Z",
                        "type": "Epic",
                        "labels": [],
                        "milestone": None,
                        "isEpic": True,
                    }
                ],
            }
        }
        data = build_report_data(
            catalog=CATALOG_FIXTURE,
            registry={"harnesses": []},
            current_machine="local",
            project_registry={},
            conversations_root=None,
            github_owner="example",
            github_data=fake_github,
            generated_at="2026-08-07T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template.html"
            template.write_text(
                '<script id="workstream-report-data" type="application/json">__WORKSTREAM_REPORT_DATA__</script>',
                encoding="utf-8",
            )
            html = render_report(data, template_path=template)
        self.assertNotIn("__WORKSTREAM_REPORT_DATA__", html)
        self.assertNotIn("</script><script>alert", html)
        self.assertIn("\\u003c/script\\u003e", html)
        self.assertEqual(ISSUE_PREVIEW_LIMIT, data["issuePreviewLimit"])
        self.assertEqual(2, len(data["workstreams"]))

    def test_public_engine_loads_catalog_instead_of_embedding_one(self) -> None:
        root = Path(__file__).resolve().parents[1]
        public_surface = "\n".join(
            (root / relative).read_text(encoding="utf-8")
            for relative in (
                "agent_bridge/workstream_report.py",
                "agent_bridge/workstream_report_template.html",
                "README.md",
            )
        )
        self.assertNotIn("WORKSTREAMS: list", public_surface)
        self.assertNotIn("INVENTORY_SNAPSHOT =", public_surface)
        self.assertIn("SharedAgentConversations/reports/workstream-control-center/catalog.json", public_surface)


if __name__ == "__main__":
    unittest.main()
