#!/usr/bin/env python3
"""Enforce the AGENTS.md line budget (AGENTS.md is a budgeted index)."""

from __future__ import annotations

import sys
from pathlib import Path

# Post-overhaul budget: AGENTS.md is 404 lines; the headroom is deliberate
# and may only shrink over time — raising MAX_LINES requires explicit
# justification in the PR that does it.
MAX_LINES = 420


def run_check(paths: list[str]) -> int:
    """Verify each AGENTS.md argument stays within MAX_LINES.

    Args:
        paths (list[str]): Staged file paths from pre-commit. Anything that is
            not AGENTS.md (basename match) is skipped silently, so this hook
            composes with the ``files:`` filter.

    Returns:
        int: Exit code (0 = within budget, 1 = over budget or unreadable).
    """
    failed = False
    for arg in paths:
        if Path(arg).name != "AGENTS.md":
            continue
        try:
            line_count = len(Path(arg).read_text(encoding="utf-8").splitlines())
        except OSError as exc:
            print(f"AGENTS.md budget: cannot read {arg}: {exc.strerror or exc}")
            failed = True
            continue
        if line_count > MAX_LINES:
            print(
                f"{arg}: {line_count} lines exceeds the AGENTS.md budget of {MAX_LINES}.\n"
                "AGENTS.md is a budgeted index: prune content and link to detail\n"
                "files instead of pasting them, or consciously edit MAX_LINES in\n"
                "scripts/hooks/check_agents_md_budget.py with justification in the PR."
            )
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_check(sys.argv[1:]))
