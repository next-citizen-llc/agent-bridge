## What this changes

<!-- The behavior difference, not the diff. -->

## Why

<!-- What was wrong or missing. Link an issue if there is one. -->

## Failure paths

<!-- What happens when the target is logged out, the worktree is dirty, the
     network is gone, or the subprocess returns nothing? "Not applicable" is a
     fine answer when it genuinely is. -->

## Checks

- [ ] `python3 -m py_compile agent_bridge/*.py`
- [ ] `python3 -m unittest discover -s tests`
- [ ] `git diff --check`
- [ ] Tests added that would fail without this change
- [ ] No new runtime dependency
- [ ] No change that widens dispatch authority, or the widening is called out above
