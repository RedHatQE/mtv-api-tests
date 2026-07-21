#!/usr/bin/env python3
"""Ban test files placed directly under tests/ (require feature subdirs)."""

from __future__ import annotations

import sys
from pathlib import Path

from _ast_utils import FindingCollector, iter_py_files, main_runner
from baseline import repo_relative_posix


def _is_tests_root_test_file(path: Path) -> bool:
    """Return True if ``path`` is repo-root ``tests/test_*.py`` (no subdirectory).

    Only ``tests/`` at the repository root is considered. Nested trees such as
    ``plugins/.../tests/test_*.py`` are not flagged.

    Args:
        path (Path): Candidate file path.

    Returns:
        bool: Whether the file is a disallowed root-level test module.
    """
    rel = Path(repo_relative_posix(path))
    return rel.parent == Path("tests") and rel.name.startswith("test_") and rel.suffix == ".py"


def _iter_candidate_paths(paths: list[str]) -> list[Path]:
    """Resolve paths to check: explicit argv files/dirs, or all under tests/.

    Explicit directory arguments are expanded to ``*.py`` files (via
    ``iter_py_files``). Explicit ``.py`` files are kept as-is. Empty argv scans
    ``tests/`` recursively.

    Args:
        paths (list[str]): File or directory paths from pre-commit (empty =
            scan tests/).

    Returns:
        list[Path]: Candidate paths to evaluate.
    """
    if paths:
        return list(iter_py_files(paths))

    tests_dir = Path("tests")
    if not tests_dir.is_dir():
        return []
    return sorted(tests_dir.rglob("*.py"))


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Flag test modules living directly in tests/ without a feature subdir.

    Args:
        paths (list[str]): File or directory paths from pre-commit (empty =
            scan tests/).
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
