## What this changes

<!-- The behavior difference, not the diff. -->

## Why

<!-- What was wrong or missing. Link an issue if there is one. -->

## Failure paths

<!-- What happens when the target is logged out, the worktree is dirty, the
     network is gone, or the subprocess returns nothing? "Not applicable" is a
     fine answer when it genuinely is. -->

## Checks

- [ ] `python3.11 -m py_compile agent_bridge/*.py` (or another Python 3.11+ interpreter)
- [ ] `python3.11 -m unittest discover -s tests` (or another Python 3.11+ interpreter)
- [ ] `ruff check agent_bridge tests` and `mypy` (incremental: `state_sync.py` only)
- [ ] `git diff --check`
- [ ] Tests added that would fail without this change
- [ ] No new runtime dependency
- [ ] No change that widens dispatch authority, or the widening is called out above
