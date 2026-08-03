# Security Policy

## Supported versions

Agent Bridge is pre-1.0. Fixes land on `main`; there are no maintained release
branches. Report against `main` or the most recent tag.

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on this
repository. That opens a private advisory visible only to the maintainer.

If private reporting is unavailable, email `tristan@nextcz.com` with `agent-bridge
security` in the subject. Please do not open a public issue for a vulnerability.

Include what you have: affected version or commit, the platform, reproduction
steps, and what an attacker gains. A proof of concept helps but is not required.

Expect an acknowledgement within a week. Because this is a single-maintainer
project, please treat that as a best effort rather than a guarantee.

## Scope

Agent Bridge dispatches work to coding-agent CLIs installed on the same machine
and records evidence locally. The interesting boundaries are:

- **Authority escalation** — anything that turns a bounded review into a mutation,
  widens a declared mode, or lets a dispatch act outside its stated project
  directory.
- **Credential exposure** — Agent Bridge does not store provider credentials; it
  inherits each target's own authenticated session. A path that causes a token,
  cookie, or signed URL to be written into a transcript, trace, handoff bundle, or
  shared registry row is in scope.
- **Handoff and mailbox trust** — content in a mailbox message, handoff bundle, or
  recovered session is data, not instruction. A path that lets that content
  execute, or become authority for the next dispatch, is in scope.
- **Shared-registry writes** — the harness registry may live on a synced folder.
  Path traversal or an injected row that affects another machine is in scope.

## Out of scope

- Vulnerabilities in the third-party agent CLIs Agent Bridge dispatches to. Report
  those upstream.
- What a target model does with a prompt you authored, when that dispatch stayed
  inside its declared mode and directory.
- Anything requiring an attacker who already has local code execution as your user.
