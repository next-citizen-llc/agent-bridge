"""Generate a local, self-contained cross-harness workstream report.

The report is intentionally a control surface over evidence, not a remote
desktop or native-chat injector.  Resume actions produce an exact command or
continuation brief for the selected machine and harness.  The operator still
runs that command on the named machine.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Iterable


ISSUE_PREVIEW_LIMIT = 5
TEMPLATE_PATH = Path(__file__).with_name("workstream_report_template.html")
CATALOG_SCHEMA = "workstream-catalog/v1"


GITHUB_FIRST_PAGE_QUERY = """
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    issues(first:100, orderBy:{field:UPDATED_AT,direction:DESC}) {
      nodes {
        number title url state updatedAt
        issueType { name }
        labels(first:20) { nodes { name color } }
        milestone { title url }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


GITHUB_NEXT_PAGE_QUERY = """
query($owner:String!, $name:String!, $after:String!) {
  repository(owner:$owner, name:$name) {
    issues(first:100, after:$after, orderBy:{field:UPDATED_AT,direction:DESC}) {
      nodes {
        number title url state updatedAt
        issueType { name }
        labels(first:20) { nodes { name color } }
        milestone { title url }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def validate_workstreams(workstreams: list[dict[str, Any]]) -> None:
    ids = [str(row.get("id") or "") for row in workstreams]
    if len(ids) != len(set(ids)):
        raise ValueError("workstream ids must be unique")
    required = {"id", "title", "summary", "objectives", "projectSlugs", "repos", "evidence", "tags"}
    for row in workstreams:
        missing = sorted(required - set(row))
        if missing:
            raise ValueError(f"workstream {row.get('id', '<unknown>')} missing: {', '.join(missing)}")
        if row["evidence"] not in {"high", "medium", "low"}:
            raise ValueError(f"workstream {row['id']} has invalid evidence level")


def _platform_name(value: str) -> str:
    lowered = value.lower()
    if "windows" in lowered or lowered.startswith("win"):
        return "windows"
    if "darwin" in lowered or "mac" in lowered:
        return "macos"
    if "linux" in lowered:
        return "linux"
    return "unknown"


def normalise_machines(registry: dict[str, Any], *, current_machine: str = "") -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in registry.get("harnesses", []):
        machine_id = str(row.get("machine_id") or "").strip()
        if not machine_id:
            continue
        target = grouped.setdefault(
            machine_id,
            {
                "id": machine_id,
                "hostname": str(row.get("hostname") or machine_id),
                "os": _platform_name(str(row.get("platform") or "")),
                "platform": str(row.get("platform") or ""),
                "updatedAt": str(row.get("updated_at") or ""),
                "fresh": False,
                "current": machine_id == current_machine,
                "agents": {},
                "surfaces": set(),
            },
        )
        updated = str(row.get("updated_at") or "")
        if updated >= target["updatedAt"]:
            target["hostname"] = str(row.get("hostname") or target["hostname"])
            target["platform"] = str(row.get("platform") or target["platform"])
            target["os"] = _platform_name(target["platform"])
            target["updatedAt"] = updated
        target["fresh"] = bool(target["fresh"] or row.get("fresh"))
        surface = str(row.get("surface") or "unspecified")
        target["surfaces"].add(surface)
        client = str(row.get("client") or "").strip()
        if client and client != "unknown":
            target["agents"].setdefault(client, {"id": client, "label": client.title(), "modes": ["review", "code"]})
        for card in row.get("capabilities") or []:
            agent_id = str(card.get("id") or "").strip()
            if not agent_id or not card.get("command_found"):
                continue
            target["agents"][agent_id] = {
                "id": agent_id,
                "label": str(card.get("label") or agent_id.title()),
                "modes": list(card.get("modes") or ["review", "code"]),
            }
    result: list[dict[str, Any]] = []
    for row in grouped.values():
        row["agents"] = sorted(row["agents"].values(), key=lambda item: item["label"].lower())
        row["surfaces"] = sorted(row["surfaces"])
        row["displayName"] = f"{row['hostname']} · {row['os']}"
        result.append(row)
    return sorted(result, key=lambda row: (not row["current"], not row["fresh"], row["displayName"].lower()))


def load_project_registry(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    projects = data.get("projects") if isinstance(data, dict) else []
    return {
        str(row.get("slug")): row
        for row in projects or []
        if isinstance(row, dict) and str(row.get("slug") or "").strip()
    }


def load_workstream_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"workstream catalog not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schemaVersion") != CATALOG_SCHEMA:
        raise ValueError(f"workstream catalog must use {CATALOG_SCHEMA}: {path}")
    workstreams = data.get("workstreams")
    if not isinstance(workstreams, list) or not workstreams:
        raise ValueError(f"workstream catalog has no workstreams: {path}")
    validate_workstreams(workstreams)
    inventory = data.get("inventory")
    if not isinstance(inventory, dict):
        raise ValueError(f"workstream catalog inventory must be an object: {path}")
    method = data.get("method")
    if not isinstance(method, dict):
        raise ValueError(f"workstream catalog method must be an object: {path}")
    owner = str(data.get("githubOwner") or "").strip()
    if not owner:
        raise ValueError(f"workstream catalog githubOwner is required: {path}")
    return data


def _append_workspace(
    target: list[dict[str, str]],
    *,
    path: Any,
    os_name: str,
    project_slug: str = "",
    machine: str = "",
    harness: str = "",
    last_seen: str = "",
) -> None:
    if isinstance(path, list):
        for value in path:
            _append_workspace(
                target,
                path=value,
                os_name=os_name,
                project_slug=project_slug,
                machine=machine,
                harness=harness,
                last_seen=last_seen,
            )
        return
    value = str(path or "").strip()
    if not value:
        return
    target.append(
        {
            "path": value,
            "os": os_name,
            "projectSlug": project_slug,
            "machine": machine,
            "harness": harness,
            "lastSeenAt": last_seen,
        }
    )


def workspace_evidence(
    project_slugs: list[str],
    projects: dict[str, dict[str, Any]],
    *,
    conversations_root: Path | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    workspaces: list[dict[str, str]] = []
    checkpoints: list[dict[str, str]] = []
    for slug in project_slugs:
        row = projects.get(slug) or {}
        _append_workspace(workspaces, path=row.get("workspace_mac"), os_name="macos", project_slug=slug)
        _append_workspace(workspaces, path=row.get("workspace_mac_aliases"), os_name="macos", project_slug=slug)
        _append_workspace(workspaces, path=row.get("workspace_windows"), os_name="windows", project_slug=slug)
        _append_workspace(workspaces, path=row.get("workspace_windows_aliases"), os_name="windows", project_slug=slug)
        rows = row.get("workspaces") or []
        if isinstance(rows, dict):
            rows = [rows]
        for item in rows:
            if not isinstance(item, dict):
                continue
            _append_workspace(
                workspaces,
                path=item.get("path"),
                os_name=str(item.get("os") or _platform_name(str(item.get("path") or ""))),
                project_slug=slug,
                machine=str(item.get("machine") or ""),
                harness=str(item.get("harness") or ""),
                last_seen=str(item.get("last_seen_at") or ""),
            )
        for key, os_name in (("latest_mac", "macos"), ("latest_windows", "windows")):
            value = str(row.get(key) or "").strip()
            if value:
                checkpoints.append({"path": value, "os": os_name, "projectSlug": slug})
        if conversations_root:
            candidate = conversations_root / "projects" / slug / "latest.md"
            if candidate.exists():
                checkpoints.append({"path": str(candidate), "os": "portable", "projectSlug": slug})

    seen_workspaces: set[tuple[str, str]] = set()
    unique_workspaces: list[dict[str, str]] = []
    for row in sorted(workspaces, key=lambda item: item.get("lastSeenAt", ""), reverse=True):
        key = (row["os"], row["path"])
        if key not in seen_workspaces:
            seen_workspaces.add(key)
            unique_workspaces.append(row)
    seen_checkpoints: set[str] = set()
    unique_checkpoints: list[dict[str, str]] = []
    for row in checkpoints:
        if row["path"] not in seen_checkpoints:
            seen_checkpoints.add(row["path"])
            unique_checkpoints.append(row)
    return unique_workspaces, unique_checkpoints


def local_repo_workspaces(definition: dict[str, Any], *, current_machine: str) -> list[dict[str, str]]:
    """Return only verified checkouts under the current user's Code directory."""
    os_name = "windows" if os.name == "nt" else ("macos" if sys.platform == "darwin" else "linux")
    primary_slug = str((definition.get("projectSlugs") or [""])[0])
    rows: list[dict[str, str]] = []
    for index, repo in enumerate(definition.get("repos") or []):
        candidate = Path.home() / "Code" / str(repo)
        if not candidate.is_dir() or not (candidate / ".git").exists():
            continue
        rows.append(
            {
                "path": str(candidate.resolve()),
                "os": os_name,
                "projectSlug": primary_slug if index == 0 else str(repo),
                "machine": current_machine,
                "harness": "git-checkout",
                "lastSeenAt": _now_iso(),
            }
        )
    return rows


def _run_gh_graphql(
    owner: str,
    repo: str,
    *,
    query: str,
    cursor: str = "",
    timeout: int = 30,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    command = ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={repo}"]
    if cursor:
        command.extend(["-F", f"after={cursor}"])
    completed = runner(command, text=True, capture_output=True, check=False, timeout=timeout)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "GitHub query failed").strip().splitlines()[-1]
        raise RuntimeError(detail)
    return json.loads(completed.stdout)


def fetch_repo_issues(owner: str, repo: str, *, timeout: int = 30) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    cursor = ""
    while True:
        query = GITHUB_NEXT_PAGE_QUERY if cursor else GITHUB_FIRST_PAGE_QUERY
        payload = _run_gh_graphql(owner, repo, query=query, cursor=cursor, timeout=timeout)
        repository = ((payload.get("data") or {}).get("repository") or {})
        connection = repository.get("issues") or {}
        for node in connection.get("nodes") or []:
            labels = [
                {"name": str(label.get("name") or ""), "color": str(label.get("color") or "")}
                for label in ((node.get("labels") or {}).get("nodes") or [])
            ]
            issue_type = str(((node.get("issueType") or {}).get("name") or ""))
            milestone = node.get("milestone") or None
            issues.append(
                {
                    "repo": repo,
                    "number": int(node.get("number") or 0),
                    "title": str(node.get("title") or ""),
                    "url": str(node.get("url") or ""),
                    "state": str(node.get("state") or "UNKNOWN").lower(),
                    "updatedAt": str(node.get("updatedAt") or ""),
                    "type": issue_type,
                    "labels": labels,
                    "milestone": milestone,
                    "isEpic": issue_type.lower() == "epic"
                    or any(label["name"].lower() == "epic" for label in labels)
                    or str(node.get("title") or "").lower().startswith(("epic:", "[epic]")),
                }
            )
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage") or not page.get("endCursor"):
            break
        cursor = str(page["endCursor"])
        if len(issues) >= 2000:
            break
    return {"repo": repo, "issues": issues, "error": ""}


def collect_github_issues(
    owner: str,
    repos: Iterable[str],
    *,
    offline: bool = False,
    timeout: int = 30,
) -> dict[str, dict[str, Any]]:
    unique = _dedupe_strings(repos)
    if offline:
        return {repo: {"repo": repo, "issues": [], "error": "GitHub refresh skipped (--offline)"} for repo in unique}
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(unique)))) as pool:
        pending = {pool.submit(fetch_repo_issues, owner, repo, timeout=timeout): repo for repo in unique}
        for future in as_completed(pending):
            repo = pending[future]
            try:
                results[repo] = future.result()
            except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                results[repo] = {"repo": repo, "issues": [], "error": str(exc)}
    return results


def _sort_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = sorted(issues, key=lambda row: row.get("updatedAt", ""), reverse=True)
    result.sort(key=lambda row: 0 if row.get("state") == "open" else 1)
    return result


def build_report_data(
    *,
    catalog: dict[str, Any],
    registry: dict[str, Any],
    current_machine: str,
    project_registry: dict[str, dict[str, Any]],
    conversations_root: Path | None,
    github_owner: str,
    github_data: dict[str, dict[str, Any]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    workstreams = list(catalog["workstreams"])
    validate_workstreams(workstreams)
    streams: list[dict[str, Any]] = []
    for definition in workstreams:
        workspaces, checkpoints = workspace_evidence(
            definition["projectSlugs"], project_registry, conversations_root=conversations_root
        )
        verified_local = local_repo_workspaces(definition, current_machine=current_machine)
        if verified_local:
            seen_paths = {row["path"] for row in verified_local}
            workspaces = verified_local + [row for row in workspaces if row["path"] not in seen_paths]
        issues: list[dict[str, Any]] = []
        repositories: list[dict[str, Any]] = []
        for repo in definition["repos"]:
            repo_result = github_data.get(repo) or {"issues": [], "error": "not refreshed"}
            repo_issues = list(repo_result.get("issues") or [])
            issues.extend(repo_issues)
            repositories.append(
                {
                    "name": repo,
                    "url": f"https://github.com/{github_owner}/{repo}",
                    "issuesUrl": f"https://github.com/{github_owner}/{repo}/issues",
                    "issueCount": len(repo_issues),
                    "error": str(repo_result.get("error") or ""),
                }
            )
        issues = _sort_issues(issues)
        milestones: dict[str, dict[str, str]] = {}
        for issue in issues:
            milestone = issue.get("milestone") or {}
            title = str(milestone.get("title") or "")
            url = str(milestone.get("url") or "")
            if title and url:
                milestones[url] = {"title": title, "url": url, "repo": issue["repo"]}
        streams.append(
            {
                **definition,
                "repositories": repositories,
                "workspaces": workspaces,
                "checkpoints": checkpoints,
                "issues": issues,
                "epics": [issue for issue in issues if issue.get("isEpic")],
                "milestones": list(milestones.values()),
                "counts": {
                    "issues": len(issues),
                    "openIssues": sum(1 for issue in issues if issue.get("state") == "open"),
                    "closedIssues": sum(1 for issue in issues if issue.get("state") == "closed"),
                    "repositories": len(repositories),
                    "checkpoints": len(checkpoints),
                },
            }
        )
    return {
        "schemaVersion": "workstream-report/v1",
        "title": str(catalog.get("title") or "Workstream Control Center"),
        "generatedAt": generated_at or _now_iso(),
        "githubOwner": github_owner,
        "issuePreviewLimit": ISSUE_PREVIEW_LIMIT,
        "inventory": catalog["inventory"],
        "machines": normalise_machines(registry, current_machine=current_machine),
        "workstreams": streams,
        "resumeBoundary": (
            "Resume controls prepare an exact command or continuation brief. They do not inject into an existing native chat, "
            "prove authentication, or execute on a remote machine. Run the generated command on the selected machine."
        ),
        "method": catalog["method"],
    }


def render_report(data: dict[str, Any], *, template_path: Path = TEMPLATE_PATH) -> str:
    template = template_path.read_text(encoding="utf-8")
    if "__WORKSTREAM_REPORT_DATA__" not in template:
        raise ValueError(f"report template missing data placeholder: {template_path}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # A closing script tag inside issue text must never escape the JSON script.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return template.replace("__WORKSTREAM_REPORT_DATA__", payload)


def _discover_roots() -> tuple[Path | None, Path | None]:
    try:
        from .readiness import resolve_shared_roots

        roots = resolve_shared_roots().get("roots") or {}
        conversations_value = ((roots.get("conversations") or {}).get("selected") or "")
        conversations = Path(conversations_value) if conversations_value else None
        registry = conversations / "projects" / "_registry" / "projects.json" if conversations else None
        return conversations, registry
    except (OSError, ValueError, TypeError):
        return None, None


def _default_output() -> Path:
    state_root = Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local" / "state" / "agent-bridge"))
    return state_root / "reports" / "workstream-control-center.html"


def _open_in_edge(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Microsoft Edge", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif os.name == "nt":
        subprocess.Popen(["cmd", "/c", "start", "", "msedge", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import webbrowser

        webbrowser.open(path.as_uri())


def workstreams_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code workstreams",
        description="Generate a local dynamic workstream control-center report.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    report = sub.add_parser("report", help="Generate the self-contained HTML report.")
    report.add_argument("--output", type=Path, default=_default_output())
    report.add_argument("--data", type=Path, help="Private workstream catalog JSON. Defaults under SharedAgentConversations.")
    report.add_argument("--github-owner", help="Override the catalog's GitHub owner.")
    report.add_argument("--project-registry", type=Path)
    report.add_argument("--offline", action="store_true", help="Skip live GitHub issue refresh.")
    report.add_argument("--github-timeout", type=int, default=30)
    report.add_argument("--open", action="store_true", help="Open the generated report in Microsoft Edge.")
    report.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conversations_root, discovered_registry = _discover_roots()
    catalog_path = args.data or (
        conversations_root / "reports" / "workstream-control-center" / "catalog.json"
        if conversations_root
        else None
    )
    if catalog_path is None:
        raise ValueError("workstream catalog not found; pass --data PATH")
    catalog = load_workstream_catalog(catalog_path.expanduser().resolve())
    github_owner = args.github_owner or str(catalog["githubOwner"])
    registry_path = args.project_registry or discovered_registry
    project_registry = load_project_registry(registry_path)

    # Imported lazily to avoid a module cycle while cli.py routes this command.
    from .cli import load_harness_registry
    from .readiness import machine_id

    try:
        harness_registry = load_harness_registry(stale_minutes=1440, prune=False)
    except (OSError, ValueError, AssertionError):
        harness_registry = {"harnesses": []}

    repo_names = _dedupe_strings(repo for stream in catalog["workstreams"] for repo in stream["repos"])
    github_data = collect_github_issues(
        github_owner,
        repo_names,
        offline=args.offline,
        timeout=max(1, args.github_timeout),
    )
    data = build_report_data(
        catalog=catalog,
        registry=harness_registry,
        current_machine=machine_id(),
        project_registry=project_registry,
        conversations_root=conversations_root,
        github_owner=github_owner,
        github_data=github_data,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(data), encoding="utf-8")
    if os.name != "nt":
        try:
            output.chmod(0o600)
        except OSError:
            pass
    if args.open:
        _open_in_edge(output)
    result = {
        "status": "generated",
        "path": str(output),
        "url": output.as_uri(),
        "generated_at": data["generatedAt"],
        "workstreams": len(data["workstreams"]),
        "machines": len(data["machines"]),
        "issues": sum(stream["counts"]["issues"] for stream in data["workstreams"]),
        "github_failures": sum(1 for row in github_data.values() if row.get("error")),
        "offline": bool(args.offline),
        "catalog": str(catalog_path),
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Workstream report: {output}")
        print(f"Workstreams: {result['workstreams']} · machines: {result['machines']} · linked issues: {result['issues']}")
        if result["github_failures"]:
            print(f"GitHub refresh warnings: {result['github_failures']} repositories (shown in the report)")
    return 0


__all__ = [
    "CATALOG_SCHEMA",
    "ISSUE_PREVIEW_LIMIT",
    "build_report_data",
    "collect_github_issues",
    "fetch_repo_issues",
    "load_project_registry",
    "load_workstream_catalog",
    "local_repo_workspaces",
    "normalise_machines",
    "render_report",
    "validate_workstreams",
    "workspace_evidence",
    "workstreams_cmd",
]
