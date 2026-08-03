# Contributing

## Design constraints

These are the properties that make the tool what it is. A change that breaks one
of them needs to argue for itself explicitly.

- **No runtime dependencies.** `dependencies` in `pyproject.toml` is empty and
  should stay that way. Agent Bridge runs as a local CLI on machines whose Python
  environment it does not control, so the standard library is the contract.
- **Local-first.** State lives under the local state directory. Nothing is
  uploaded to a service Agent Bridge operates.
- **Bounded authority.** A dispatch carries an explicit mode and limits. Code that
  widens authority silently — turning a review into a mutation, inheriting
  permission from conversational tone, treating a target's session as transferable
  — is the one class of change that will be rejected on principle.
- **Typed failures.** Prefer a specific failure class over a boolean. See
  [the failure taxonomy](docs/field-notes/failure-taxonomy.md) for why.

## Setup

Python 3.11 or newer. No install step is required to run the tests.

```bash
git clone https://github.com/next-citizen-llc/agent-bridge
cd agent-bridge
python3 -m py_compile agent_bridge/*.py
python3 -m unittest discover -s tests
```

To exercise the CLI without installing it, `bin/agent` runs from the checkout.

## Before opening a pull request

```bash
python3 -m py_compile agent_bridge/*.py
python3 -m unittest discover -s tests
git diff --check
```

CI runs the suite on Python 3.11 through 3.14, builds the distribution, and
smoke-tests the Windows installer end to end. All of it must pass.

## What a good change looks like

- **Tests that would fail before the change.** A test that passes either way
  documents behavior rather than protecting it.
- **A failure path.** What happens when the target is logged out, the worktree is
  dirty, the network is gone, or the subprocess returns nothing?
- **No new external state.** If a change needs to record something, it goes in the
  existing trace, findings, verdict, or usage records rather than a new file.
- **Commit messages that say why.** The diff already says what.

## Reporting a security issue

Do not open a public issue. See [SECURITY.md](SECURITY.md).
