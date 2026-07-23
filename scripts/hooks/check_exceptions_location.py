#!/usr/bin/env python3
"""Require custom Exception subclasses to live in exceptions/exceptions.py."""

from __future__ import annotations

import ast
import sys
from collections import Counter
from functools import partial
from pathlib import Path

from _ast_utils import FindingCollector, for_each_parsed_file, main_runner, walk_with_stack
from baseline import is_baselined, load_baseline_for_check, repo_relative_posix

_EXCEPTION_BASE_NAMES: frozenset[str] = frozenset({
    "Exception",
    "BaseException",
    "ExceptionGroup",
    "BaseExceptionGroup",
    "Warning",
})


def _is_allowed_file(path: Path) -> bool:
    """Return True if ``path`` is the allowed exceptions module.

    Args:
        path (Path): Source file path.

    Returns:
        bool: Whether this is exactly ``exceptions/exceptions.py`` at repo root.
    """
    return repo_relative_posix(path) == "exceptions/exceptions.py"


def _base_identifier(base: ast.expr) -> str | None:
    """Return the Name.id or Attribute.attr for a ClassDef base, if present.

    Args:
        base (ast.expr): A ClassDef base expression.

    Returns:
        str | None: Identifier used for matching, or None if unsupported form.
    """
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _base_is_exception_like(base: ast.expr) -> bool:
    """Return True if ``base`` is a known exception base or ends with Error/Exception/Warning.

    Recognizes ``Exception``, ``BaseException``, ``ExceptionGroup``,
    ``BaseExceptionGroup``, ``Warning``, and names ending with ``Error``,
    ``Exception``, or ``Warning``.

    Args:
        base (ast.expr): A ClassDef base expression.

    Returns:
        bool: Whether the base looks like an exception type.
    """
    name = _base_identifier(base)
    if name is None:
        return False
    return name in _EXCEPTION_BASE_NAMES or name.endswith(("Error", "Exception", "Warning"))


def _check_file(
    path: Path,
    tree: ast.AST,
    collector: FindingCollector,
    baseline: Counter[tuple[str, str]],
) -> None:
    """Flag exception-like subclasses defined outside exceptions/exceptions.py.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
        baseline (Counter[tuple[str, str]]): Grandfathered path/fingerprint
            occurrence counts.
    """
    if _is_allowed_file(path):
        return

    for node, _stack in walk_with_stack(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(_base_is_exception_like(base) for base in node.bases):
            if is_baselined(path, node.lineno, baseline, end_lineno=node.end_lineno):
                continue
            collector.report(
                path,
                node.lineno,
                f"custom Exception subclass '{node.name}' must be defined in exceptions/exceptions.py",
            )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the exceptions location check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink.
    """
    baseline = load_baseline_for_check("check_exceptions_location", collector.report)
    if baseline is None:
        return

    for_each_parsed_file(paths, collector, partial(_check_file, baseline=baseline))


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
