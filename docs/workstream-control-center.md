# Workstream control center

`agent code workstreams report` turns a private workstream catalog, the portable
project registry, current Agent Bridge capability cards, and GitHub issue data
into one self-contained local HTML report.

The report is a preparation and evidence surface. It can copy a bounded Agent
Bridge command, create-command for the durable task ledger, or native handoff
brief. It does not attach to an existing chat, silently run a command, prove
that a remote harness is authenticated, or make vendor-native histories
interchangeable.

## Inputs and privacy boundary

The engine and HTML template are packaged with Agent Bridge. Workstream data is
not. By default, the CLI discovers:

```text
SharedAgentConversations/
  projects/_registry/projects.json
  reports/workstream-control-center/catalog.json
```

The catalog and generated report can contain private paths, objectives, issue
titles, and checkpoint locations. Keep both outside a public Git checkout. On
POSIX systems, Agent Bridge writes the report with mode `0600` on a best-effort
basis.

The catalog contract is illustrated by
[`docs/examples/workstream-catalog.example.json`](examples/workstream-catalog.example.json).
Each workstream requires:

- `id`, `title`, and `summary`;
- `objectives` and `tags`;
- `projectSlugs` used to resolve portable workspace and checkpoint evidence;
- `repos` used for a repository-wide GitHub issue inventory; and
- `evidence`, one of `high`, `medium`, or `low`.

Two optional fields express exact GitHub relationships:

- `issueRefs` for directly related issues;
- `epicRefs` for directly related native GitHub Epics.

Each reference may be `repo#123`, `owner/repo#123`, a GitHub issue URL, or an
object such as `{"repo":"example-runtime","number":123}`. Exact references
are displayed separately from the broader repository inventory. An explicit
owner must match the catalog's `githubOwner`; cross-owner references remain
unresolved rather than being silently queried against the wrong owner. An `epicRefs`
entry that resolves to a non-Epic stays visible as unresolved evidence instead
of being mislabeled.

## Generate the report

With the default private catalog:

```bash
agent code workstreams report
```

Open the result in Microsoft Edge:

```bash
agent code workstreams report --open
```

Use explicit inputs or destinations when discovery is not appropriate:

```bash
agent code workstreams report \
  --data /private/path/catalog.json \
  --project-registry /private/path/projects.json \
  --output /private/path/workstreams.html \
  --json
```

Other useful options:

- `--offline` omits live GitHub refresh and records that limitation;
- `--github-owner OWNER` overrides the catalog owner;
- `--github-timeout SECONDS` bounds each repository query; and
- `--open` launches the resulting file in Edge on macOS or Windows.

The default output is:

```text
~/.local/state/agent-bridge/reports/workstream-control-center.html
```

## What generation does

1. Validate the catalog schema and required workstream fields.
2. Read the project registry and resolve workspace/checkpoint evidence.
3. Read harness registrations without pruning stale rows.
4. Offer agents only when a capability card positively reports that its
   command exists on the selected machine.
5. Fetch GitHub issues with pagination, including native issue type, labels,
   milestone, state, and URL.
6. Resolve explicit issue and Epic references independently from repository
   membership.
7. Escape the embedded JSON payload and render it into the packaged template.
8. Write one self-contained local HTML file.

Repository issue association is intentionally broad. It means “issues in a
mapped repository,” not “semantically related to this objective.” Only an
explicit `issueRefs` or `epicRefs` entry earns the “Related GitHub work” label.

## Resume semantics

The resume dialog chooses a machine, a positively discovered agent, an
execution surface, and a mode. Workspace routing never falls back across
operating systems. If a matching path or agent is missing, the report says so
and withholds an executable command.

On Windows, copied commands are explicitly labeled and rendered for PowerShell.
On macOS and Linux, they use POSIX-shell quoting. A remote-machine choice still
produces only text to copy; the operator runs it on that machine.

Capability cards and registry heartbeats are routing evidence, not readiness
proof. The receiver must re-check the worktree, GitHub state, artifacts, live
credentials, and time-sensitive facts before changing state.

## Validate the executable artifact

The unit suite covers catalog loading, machine normalization, GitHub
relationship resolution, workspace evidence, and safe HTML embedding:

```bash
python3 -m unittest tests/test_workstream_report.py
```

The optional browser validator requires Playwright and, when available,
`@axe-core/playwright`:

```bash
node scripts/validate-workstream-report.cjs \
  "$HOME/.local/state/agent-bridge/reports/workstream-control-center.html"
```

It exercises desktop and narrow/mobile layouts, search, expand/collapse, the
five-issue preview, machine/agent/surface selection, Windows routing, dialog
and toast viewport bounds, horizontal overflow, browser errors, and serious or
critical accessibility findings. Screenshots and the validation record remain
beside the local report rather than entering Git.

## Design basis

The source-backed research behind this boundary and the future roadmap is in
[Harness engineering for cross-machine work continuity](research/harness-engineering-workstream-continuity.md).
