#!/usr/bin/env python3
"""Ban test files placed directly under tests/ (require feature subdirs)."""

from __future__ import annotations

import sys
from pathlib import Path

from _ast_utils import FindingCollector, main_runner


def _is_tests_root_test_file(path: Path) -> bool:
    """Return True if ``path`` is ``tests/test_*.py`` with no subdirectory.

    Args:
        path (Path): Candidate file path.

    Returns:
        bool: Whether the file is a disallowed root-level test module.
    """
    return path.parent.name == "tests" and path.name.startswith("test_") and path.suffix == ".py"


def _iter_candidate_paths(paths: list[str]) -> list[Path]:
    """Resolve paths to check: explicit argv files, or all under tests/.

    Args:
        paths (list[str]): File paths from pre-commit (empty = scan tests/).

    Returns:
        list[Path]: Candidate paths to evaluate.
    """
    if paths:
        return [Path(p) for p in paths if Path(p).suffix == ".py"]

    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        return []
    return sorted(tests_dir.rglob("*.py"))


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Flag test modules living directly in tests/ without a feature subdir.

    Args:
        paths (list[str]): File paths from pre-commit (empty = scan tests/).
        collector (FindingCollector): Finding sink.
    """
    for path in _iter_candidate_paths(paths):
        if _is_tests_root_test_file(path):
            collector.report(
                path,
                1,
                "test files must live under tests/<feature>/, not directly in tests/",
            )


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
