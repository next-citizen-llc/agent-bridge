"""Shared runtime-floor check for direct Python entrypoints."""

import sys


MINIMUM_PYTHON = (3, 11)


def require_supported_python(entrypoint, version_info=None):
    """Exit actionably before an entrypoint imports Python 3.11-only modules."""
    current = tuple(version_info or sys.version_info[:3])
    if current >= MINIMUM_PYTHON:
        return
    found = ".".join(str(part) for part in current[:3])
    required = ".".join(str(part) for part in MINIMUM_PYTHON)
    print(
        f"{entrypoint}: Python {required} or newer is required (found {found}). "
        "Select a supported interpreter or set AGENT_BRIDGE_PYTHON.",
        file=sys.stderr,
    )
    raise SystemExit(1)
