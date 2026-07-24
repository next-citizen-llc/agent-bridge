# Safety model

Agent Bridge makes authority and evidence visible around local coding-agent invocations. It reduces
ambiguity; it does not make an untrusted model, plugin, prompt, repository, or vendor service safe by
itself.

## Assets to protect

- source code and repository integrity;
- credentials and authenticated developer sessions;
- private prompts, transcripts, and continuation evidence;
- production systems and external accounts;
- the accuracy of task, finding, verdict, and readiness records.

## Main failure and threat classes

| Risk | Control in Agent Bridge | Residual responsibility |
| --- | --- | --- |
| Implicit write authority | Separate `review` and `code` modes; mode-specific target flags and task contracts | Inspect the resolved command and grant code mode only for a bounded worktree task |
| Prompt or repository injection | Hard limits are added to dispatched turns; review mode is read-only where the target supports it | Treat model output as untrusted and inspect changes before any external action |
| Credential or production misuse | Contracts prohibit credential use, deploys, teardown, live production actions, and direct GitHub pushes | Target CLIs and local shells have their own configuration; do not expose credentials unnecessarily |
| Cross-project confusion | Resolve and print the Git root, branch, head revision, status, and explicit `--project-dir` | Confirm the printed target before execution |
| False readiness | Typed readiness reports distinguish authentication, network, MCP, configuration, context, and source failures | Recheck stale or degraded evidence and use `--require-ready` when required |
| Private transcript leakage | Runtime state is outside repositories; session recovery uses bounded, redacted handoffs | Secure the local account and review handoff contents before sharing |
| Unbounded multi-agent spend | Loop turn limits, spawn policy, route policy, and budget caps | Select an appropriate policy and inspect stored traces |
| Installer side effects | Default install is launcher-only and refuses path collisions; hooks, persistent PATH changes, and MCP registration are explicit options | Inspect requested global integrations and use exact-match uninstallers |

## Authority is layered

Agent Bridge uses several layers because prompt text alone is not an authorization boundary:

1. the operator chooses a target worktree and mode;
2. the adapter applies target-specific sandbox or permission flags;
3. the dispatched task contract states hard limits;
4. task and trace records preserve what was requested and attempted;
5. external publication remains outside the bridge's authority.

No layer should be treated as a substitute for the others.

## Local-first does not mean local inference

Agent Bridge stores its coordination state locally and starts locally installed CLIs. A target CLI
may still send prompts, selected files, or tool results to its model provider according to that
tool's configuration and terms. Inspect the target command, provider policy, and repository
sensitivity before running it.

## Trust boundaries

- The mailbox MCP is intended for processes running as the same trusted local user. It is not a
  hardened multi-user service.
- Handoff artifacts are claims with provenance, not proof that the source task was correct.
- A passing verifier is evidence from another model turn, not independent security certification.
- A fresh harness heartbeat is liveness evidence for a file write, not proof of authenticated
  session availability.
- A dry run validates command construction, not target behavior.

## Safe operating sequence

1. Inspect repository status and the exact target path.
2. Start with `review` or `--dry-run`.
3. Use `agent code preflight work` for the intended client and surface.
4. Grant `code` mode only with a narrow implementation prompt.
5. Review the diff and run repository checks.
6. Perform publishing, deployment, messaging, or account changes through a separate explicitly
   authorized workflow.

## Reporting a security issue

Do not open a public issue containing credentials, private transcripts, or exploitable personal
environment details. Use GitHub's private vulnerability reporting surface if it is enabled for the
repository; otherwise contact the repository owner through a private channel listed on the owner's
GitHub profile.
